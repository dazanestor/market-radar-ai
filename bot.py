import logging
import datetime
import math
import os
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed

import asyncio
import pandas as pd
import yaml
import yfinance as yf
from zoneinfo import ZoneInfo

from telegram.ext import ApplicationBuilder, ContextTypes

from generate_csv import generate
from scoring import score_watchlist
from ai_analysis import analyze, check_api_health
from fetch_data import get_macro_context, get_news, to_eur, clear_fx_cache
from database import (
    init_db, save_snapshot, save_report,
    get_active_alerts, deactivate_alert, log_alert_triggered,
    get_unnotified_alerts, mark_alert_notified, vacuum_db,
    purge_old_price_history, purge_old_news_cache, effective,
)
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OUTPUT_DIR,
    REPORT_HOUR, TIMEZONE,
    TELEGRAM_MAX_CHARS, DRAWDOWN_ALERT_THRESHOLD,
)


def _token() -> str:
    return effective("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)


def _chat_id() -> str:
    return effective("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)

def _fmt(price):
    return f"€{price:.2f}"


def _md_escape(text: str) -> str:
    """Escapa caracteres especiales de Markdown v1 de Telegram."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


def _split_text(text, limit=TELEGRAM_MAX_CHARS):
    """Divide texto en bloques respetando saltos de línea."""
    chunks = []
    while len(text) > limit:
        split_at = text.rfind('\n', 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    if text:
        chunks.append(text)
    return chunks


async def _send_long(bot, chat_id, text, parse_mode="Markdown"):
    """Envía texto dividiéndolo en bloques. Fallback a texto plano si Markdown falla."""
    for chunk in _split_text(text):
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=chunk)


# ── Helpers ───────────────────────────────────────────────────────────────────

_KNOWN_TICKERS_KEYS = {"portfolio", "watchlist", "tr_isin_map"}


def _validate_tickers_schema(data: dict) -> dict:
    if not isinstance(data, dict):
        logging.error("tickers.yaml: estructura inválida (se esperaba un dict)")
        return {}
    result = {}
    for key, value in data.items():
        if key not in _KNOWN_TICKERS_KEYS:
            logging.warning(f"tickers.yaml: clave desconocida '{key}', ignorada")
            continue
        if key in ("portfolio", "watchlist"):
            if value is None:
                result[key] = {}
            elif isinstance(value, dict):
                result[key] = value
            else:
                logging.error(f"tickers.yaml: '{key}' debe ser un dict, ignorado")
        else:
            result[key] = value
    return result


def _load_tickers():
    try:
        with open("tickers.yaml") as f:
            data = yaml.safe_load(f) or {}
        data = _validate_tickers_schema(data)
        return {k: (v or {}) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        logging.error(f"tickers.yaml inválido: {e}")
        return {}


def _build_alerts(df):
    alerts = []
    for _, row in df.iterrows():
        if row["drawdown_52w"] < DRAWDOWN_ALERT_THRESHOLD:
            alerts.append(
                f"⚠️ *{row['ticker']}* ({row['name']}): drawdown de {row['drawdown_52w']:.1f}% desde máximo anual"
            )
        if row.get("trend") == "empeorando":
            alerts.append(f"📉 *{row['ticker']}*: drawdown en tendencia creciente esta semana")
    return alerts


async def _run_report(bot, chat_id):
    macro = get_macro_context()
    df, errors = generate()

    if df.empty:
        detail = f"\n\n⚠️ {', '.join(errors)}" if errors else ""
        await bot.send_message(chat_id=chat_id, text=f"❌ No se pudo obtener datos de ningún ticker.{detail}")
        return

    df = score_watchlist(df)
    save_snapshot(df.to_dict("records"))

    portfolio_df = df[df["category"] == "portfolio"].copy()
    watchlist_df = df[df["category"] == "watchlist"].copy()

    ticker_list = df["ticker"].tolist()
    with ThreadPoolExecutor(max_workers=8) as pool:
        news_futures = {pool.submit(get_news, t): t for t in ticker_list}
        news_by_ticker = {}
        for fut in as_completed(news_futures, timeout=120):
            t = news_futures[fut]
            try:
                news_by_ticker[t] = fut.result()
            except Exception:
                news_by_ticker[t] = []

    alerts = _build_alerts(df)

    try:
        ai_report = analyze(portfolio_df, watchlist_df, macro=macro, news_by_ticker=news_by_ticker)
    except Exception as e:
        logging.error(f"Error en Claude API: {e}")
        await bot.send_message(chat_id=chat_id, text=f"❌ Error al generar análisis con IA: {e}")
        return

    save_report(ai_report)

    message = ai_report
    if alerts:
        message = "\n".join(alerts) + "\n\n" + message
    if errors:
        message += f"\n\n⚠️ *Tickers con error:* {', '.join(errors)}"

    await _send_long(bot, chat_id, message)


# ── Jobs periódicos ───────────────────────────────────────────────────────────

async def job_daily_report(context: ContextTypes.DEFAULT_TYPE):
    logging.info("Ejecutando reporte diario...")
    try:
        await _run_report(context.bot, _chat_id())
    except Exception as e:
        logging.exception("Error en el reporte diario")
        try:
            await context.bot.send_message(
                chat_id=_chat_id(),
                text=f"⚠️ *Error en el reporte diario*\n`{type(e).__name__}: {str(e)[:200]}`",
                parse_mode="Markdown",
            )
        except Exception:
            logging.exception("No se pudo notificar el error del reporte")


async def job_check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    alerts = get_active_alerts()
    if not alerts:
        return

    tickers_needed = list({a[1] for a in alerts})
    prices = {}
    clear_fx_cache()

    csv_data = {}
    has_advanced = any((len(a) > 5 and a[5] in ("drawdown", "score")) for a in alerts)
    if has_advanced:
        try:
            csv_path = f"{OUTPUT_DIR}/precios_global.csv"
            if os.path.exists(csv_path):
                df = pd.read_csv(csv_path)
                for _, row in df.iterrows():
                    csv_data[row["ticker"]] = row.to_dict()
        except Exception:
            logging.exception("Error leyendo CSV para alertas avanzadas")

    for ticker in tickers_needed:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="2d")
            if not hist.empty:
                price_raw = hist["Close"].iloc[-1]
                try:
                    currency = (t.info or {}).get("currency", "USD")
                except Exception:
                    currency = "USD"
                prices[ticker] = to_eur(price_raw, currency)
        except Exception as e:
            logging.warning(f"Error obteniendo precio de {ticker} para alertas: {e}")

    for row in alerts:
        alert_id, ticker, target, direction = row[0], row[1], row[2], row[3]
        condition_type = row[5] if len(row) > 5 else "price"

        current = prices.get(ticker)
        if current is None or (isinstance(current, float) and math.isnan(current)):
            continue

        triggered = False
        icon = "🔔"
        msg_detail = ""

        if condition_type == "drawdown":
            dd = csv_data.get(ticker, {}).get("drawdown_52w")
            if dd is not None and not (isinstance(dd, float) and math.isnan(dd)):
                triggered = float(dd) < float(target)
                if triggered:
                    icon = "📉"
                    msg_detail = f"Drawdown: {dd:.1f}% (umbral: {target:.1f}%)"
        elif condition_type == "score":
            score = csv_data.get(ticker, {}).get("score")
            if score is not None and not (isinstance(score, float) and math.isnan(score)):
                triggered = float(score) > float(target)
                if triggered:
                    icon = "⭐"
                    msg_detail = f"Score: {score:.1f} (umbral: {target:.1f})"
        else:
            triggered = (direction == "below" and current <= target) or \
                        (direction == "above" and current >= target)
            if triggered:
                icon = "📉" if direction == "below" else "📈"
                msg_detail = (
                    f"{'bajado a' if direction == 'below' else 'subido a'} "
                    f"{_fmt(current)} (objetivo: {_fmt(target)})"
                )

        if triggered:
            history_id = None
            try:
                history_id = log_alert_triggered(
                    ticker, target, direction, current,
                    condition_type=condition_type,
                )
            except Exception:
                logging.exception("Error guardando historial de alerta")
            deactivate_alert(alert_id)
            msg = f"{icon} *Alerta disparada*\n*{_md_escape(ticker)}*: {_md_escape(msg_detail)}"
            try:
                await context.bot.send_message(
                    chat_id=_chat_id(), text=msg, parse_mode="Markdown"
                )
                if history_id:
                    mark_alert_notified(history_id)
            except Exception:
                logging.exception("Error enviando notificación de alerta %s", ticker)


async def job_replay_unnotified_alerts(context):
    """Al arrancar, reenvía alertas que se dispararon mientras el bot estaba caído."""
    pending = get_unnotified_alerts(limit=20)
    if not pending:
        return
    logging.info("Reenviando %d alerta(s) no notificada(s).", len(pending))
    for row in pending:
        history_id, ticker, target, direction, ctype, cvalue, triggered_at, price_at = row
        if ctype == "drawdown":
            detail = f"Drawdown disparó umbral {target:.1f}% el {triggered_at}"
        elif ctype == "score":
            detail = f"Score superó umbral {target:.1f} el {triggered_at}"
        else:
            arrow = "bajado a" if direction == "below" else "subido a"
            detail = f"Precio {arrow} €{price_at:.2f} el {triggered_at} (objetivo €{target:.2f})"
        msg = f"🔔 *Alerta pendiente* (bot estaba caído)\n*{_md_escape(ticker)}*: {_md_escape(detail)}"
        try:
            await context.bot.send_message(
                chat_id=_chat_id(), text=msg, parse_mode="Markdown"
            )
            mark_alert_notified(history_id)
        except Exception:
            logging.exception("Error reenviando alerta pendiente %s", ticker)


async def job_vacuum_db(context):
    """Mantenimiento semanal de SQLite: purga datos antiguos y compacta el fichero."""
    try:
        ph = purge_old_price_history(days=365)
        nc = purge_old_news_cache(days=30)
        logging.info("Purga: %d snapshots >1 año, %d traducciones >30 días.", ph, nc)
        vacuum_db()
        logging.info("VACUUM semanal completado.")
    except Exception:
        logging.exception("Error en mantenimiento semanal de BD")


async def job_check_claude_health(context):
    """Comprueba semanalmente que la API de Claude responde."""
    ok = check_api_health()
    if ok:
        logging.info("Claude API healthcheck OK.")
    else:
        logging.error("Claude API no responde.")
        try:
            await context.bot.send_message(
                chat_id=_chat_id(),
                text="⚠️ *Alerta de sistema*: La API de Claude no responde. "
                     "Revisa la clave API o el saldo de la cuenta.",
                parse_mode="Markdown",
            )
        except Exception:
            logging.exception("Error enviando alerta de salud de Claude")


async def job_check_exdividend(context: ContextTypes.DEFAULT_TYPE):
    """Verifica si algún ticker tiene fecha ex-dividendo en los próximos 3 días."""
    tickers = _load_tickers()
    all_tickers = []
    for cat in ("portfolio", "watchlist"):
        for ticker, meta in (tickers.get(cat) or {}).items():
            name = meta.get("name", ticker) if isinstance(meta, dict) else ticker
            all_tickers.append((ticker, name))

    if not all_tickers:
        return

    today = datetime.date.today()
    alerts = []

    for ticker, name in all_tickers:
        try:
            info = yf.Ticker(ticker).info or {}
            ex_date_ts = info.get("exDividendDate")
            if not ex_date_ts:
                continue
            ex_date = datetime.date.fromtimestamp(ex_date_ts)
            days_until = (ex_date - today).days
            if 0 <= days_until <= 3:
                div = info.get("dividendRate") or info.get("lastDividendValue")
                div_str = f" (${div:.2f}/acción)" if div else ""
                alerts.append(
                    f"💰 *{_md_escape(ticker)}* — {_md_escape(name)}\n"
                    f"  Ex-dividendo el {ex_date.strftime('%d/%m/%Y')} "
                    f"(en {days_until} día{'s' if days_until != 1 else ''}){div_str}"
                )
        except Exception:
            logging.debug(f"Error obteniendo ex-dividend date de {ticker}")

    if alerts:
        msg = "📅 *Próximas fechas ex-dividendo*\n\n" + "\n\n".join(alerts)
        try:
            await context.bot.send_message(
                chat_id=_chat_id(), text=msg, parse_mode="Markdown"
            )
        except Exception:
            logging.exception("Error enviando alerta ex-dividend")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()

    # Esperar hasta que TOKEN y CHAT_ID estén configurados (BD o env var)
    while True:
        token   = _token()
        chat_id = _chat_id()
        if token and chat_id:
            break
        logging.warning(
            "Bot en espera: TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados. "
            "Configúralos en el dashboard web (/settings/app). Reintentando en 60s..."
        )
        _time.sleep(60)

    # Leer REPORT_HOUR y TIMEZONE efectivos (BD > env)
    try:
        _rh = int(effective("REPORT_HOUR", str(REPORT_HOUR)))
        if not 0 <= _rh <= 23:
            _rh = REPORT_HOUR
    except ValueError:
        _rh = REPORT_HOUR
    _tz_name = effective("TIMEZONE", TIMEZONE)
    try:
        tz = ZoneInfo(_tz_name)
    except Exception:
        tz = ZoneInfo(TIMEZONE)

    app = ApplicationBuilder().token(token).build()

    report_time = datetime.time(hour=_rh, minute=0, tzinfo=tz)
    app.job_queue.run_daily(job_daily_report, time=report_time)
    exdiv_time = datetime.time(hour=7, minute=0, tzinfo=tz)
    app.job_queue.run_daily(job_check_exdividend, time=exdiv_time)
    app.job_queue.run_repeating(job_check_price_alerts, interval=3600, first=60)
    app.job_queue.run_once(job_replay_unnotified_alerts, when=30)
    app.job_queue.run_repeating(job_vacuum_db, interval=7 * 86400, first=300)
    app.job_queue.run_repeating(job_check_claude_health, interval=7 * 86400, first=120)

    logging.info(f"Bot iniciado (modo pasivo). Reporte diario a las {_rh}:00 {_tz_name}.")
    app.run_polling()


if __name__ == "__main__":
    main()
