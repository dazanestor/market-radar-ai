"""
Servicio scheduler standalone — reemplaza al bot de Telegram.
Ejecuta todos los jobs periódicos y envía notificaciones via Web Push.
"""
import logging
import math
import datetime
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import yfinance as yf
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from generate_csv import generate
from scoring import score_by_horizon
from ai_analysis import analyze, check_api_health, summarize_alerts, summarize_report
from fetch_data import get_macro_context, get_news, to_eur, clear_fx_cache
from database import (
    init_db, save_snapshot, save_report,
    get_active_alerts, deactivate_alert, log_alert_triggered,
    get_unnotified_alerts, mark_alert_notified, vacuum_db,
    purge_old_price_history, purge_old_news_cache, effective,
    get_all_positions,
    get_tickers_as_yaml_dict,
    get_latest_snapshot_as_df,
)
from config import REPORT_HOUR, TIMEZONE, DRAWDOWN_ALERT_THRESHOLD

try:
    from push_utils import send_push_to_all as _send_push
    _PUSH_AVAILABLE = True
except Exception:
    _PUSH_AVAILABLE = False
    def _send_push(*a, **kw): return 0

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)


def _fmt(price):
    return f"€{price:.2f}"


def _build_alerts(df):
    alerts = []
    for _, row in df.iterrows():
        if row["drawdown_52w"] < DRAWDOWN_ALERT_THRESHOLD:
            alerts.append(
                f"{row['ticker']} ({row['name']}): drawdown {row['drawdown_52w']:.1f}%"
            )
        if row.get("trend") == "empeorando":
            alerts.append(f"{row['ticker']}: drawdown en tendencia creciente esta semana")
    return alerts


# ── Jobs ──────────────────────────────────────────────────────────────────────

def job_daily_report():
    logging.info("Ejecutando reporte diario...")
    try:
        macro = get_macro_context()
        df, errors = generate()

        if df.empty:
            detail = f": {', '.join(errors)}" if errors else ""
            _send_push("Error en reporte diario", f"No se pudo obtener datos de ningún ticker{detail}", "/reportes")
            return

        df = score_by_horizon(df)
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
            logging.error("Error en Claude API: %s", e)
            _send_push("Error en reporte diario", f"Error al generar análisis con IA: {e}", "/reportes")
            return

        save_report(ai_report)

        body_parts = []
        if alerts:
            body_parts.append("; ".join(alerts[:2]))
        try:
            short = summarize_report(ai_report)
            if short:
                body_parts.append(short[:100])
        except Exception:
            pass
        if errors:
            body_parts.append(f"Errores: {', '.join(errors)}")

        body = " | ".join(body_parts) if body_parts else "El análisis de mercado está disponible en el dashboard."
        _send_push("Informe diario listo", body[:200], "/reportes")
        logging.info("Reporte diario completado.")

    except Exception as e:
        logging.exception("Error en el reporte diario")
        _send_push("Error en reporte diario", f"{type(e).__name__}: {str(e)[:150]}", "/reportes")


def job_check_price_alerts():
    alerts = get_active_alerts()
    if not alerts:
        return

    tickers_needed = list({a[1] for a in alerts})
    prices = {}
    triggered_msgs = []
    triggered_history = []
    portfolio_positions = {r[0]: (r[1], r[2]) for r in get_all_positions()}
    clear_fx_cache()

    csv_data = {}
    has_advanced = any((len(a) > 5 and a[5] in ("drawdown", "score", "price_pct")) for a in alerts)
    if has_advanced:
        try:
            df = get_latest_snapshot_as_df()
            if df is not None:
                for _, row in df.iterrows():
                    csv_data[row["ticker"]] = row.to_dict()
        except Exception:
            logging.exception("Error leyendo snapshot para alertas avanzadas")

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
            logging.warning("Error obteniendo precio de %s: %s", ticker, e)

    for row in alerts:
        alert_id, ticker, target, direction = row[0], row[1], row[2], row[3]
        condition_type = row[5] if len(row) > 5 else "price"
        current = prices.get(ticker)
        if current is None or (isinstance(current, float) and math.isnan(current)):
            continue

        triggered = False
        icon = "🔔"
        msg_detail = ""

        if condition_type == "price_pct":
            condition_value = row[6] if len(row) > 6 else None
            if condition_value is not None and not (isinstance(condition_value, float) and math.isnan(condition_value)):
                threshold = float(condition_value) * (1 + float(target) / 100)
                triggered = (direction == "below" and current <= threshold) or \
                            (direction == "above" and current >= threshold)
                if triggered:
                    icon = "📉" if direction == "below" else "📈"
                    msg_detail = f"{ticker}: {'bajó' if direction == 'below' else 'subió'} {target:+.1f}% (umbral {threshold:.2f}, actual {current:.2f})"
        elif condition_type == "stoploss_pct":
            pos = portfolio_positions.get(ticker)
            if pos:
                _, avg_cost = pos
                if avg_cost and not (isinstance(avg_cost, float) and math.isnan(avg_cost)) and avg_cost > 0:
                    loss_pct = (current - float(avg_cost)) / float(avg_cost) * 100
                    triggered = loss_pct < -abs(float(target))
                    if triggered:
                        icon = "🛑"
                        msg_detail = f"{ticker}: stop-loss pérdida {loss_pct:.1f}% vs coste"
        elif condition_type == "drawdown":
            dd = csv_data.get(ticker, {}).get("drawdown_52w")
            if dd is not None and not (isinstance(dd, float) and math.isnan(dd)):
                triggered = float(dd) < float(target)
                if triggered:
                    icon = "📉"
                    msg_detail = f"{ticker}: drawdown {dd:.1f}% (umbral {target:.1f}%)"
        elif condition_type == "score":
            score = csv_data.get(ticker, {}).get("score")
            if score is not None and not (isinstance(score, float) and math.isnan(score)):
                triggered = float(score) > float(target)
                if triggered:
                    icon = "⭐"
                    msg_detail = f"{ticker}: score {score:.1f} (umbral {target:.1f})"
        else:
            triggered = (direction == "below" and current <= target) or \
                        (direction == "above" and current >= target)
            if triggered:
                icon = "📉" if direction == "below" else "📈"
                msg_detail = f"{ticker}: {'bajado a' if direction == 'below' else 'subido a'} {_fmt(current)} (obj {_fmt(target)})"

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
            triggered_msgs.append(f"{icon} {msg_detail}")
            triggered_history.append(history_id)

    if triggered_msgs:
        if len(triggered_msgs) >= 2:
            try:
                summary = summarize_alerts([m for m in triggered_msgs])
                if summary:
                    triggered_msgs.insert(0, f"🤖 Resumen IA: {summary}")
            except Exception:
                logging.exception("Error generando resumen de alertas con IA")

        for history_id in triggered_history:
            if history_id:
                try:
                    mark_alert_notified(history_id)
                except Exception:
                    pass

        plain = "; ".join(m for m in triggered_msgs[:3])
        _send_push("Alerta de mercado", plain[:200], "/alertas")
        logging.info("Alertas disparadas: %d", len(triggered_msgs))


def job_replay_unnotified_alerts():
    """Al arrancar, reenvía alertas que se dispararon mientras el scheduler estaba caído."""
    pending = get_unnotified_alerts(limit=20)
    if not pending:
        return
    logging.info("Reenviando %d alerta(s) no notificada(s).", len(pending))
    msgs = []
    for row in pending:
        history_id, ticker, target, direction, ctype, cvalue, triggered_at, price_at = row
        if ctype == "drawdown":
            detail = f"{ticker}: drawdown umbral {target:.1f}% el {triggered_at}"
        elif ctype == "score":
            detail = f"{ticker}: score umbral {target:.1f} el {triggered_at}"
        else:
            arrow = "bajado a" if direction == "below" else "subido a"
            detail = f"{ticker}: {arrow} €{price_at:.2f} el {triggered_at}"
        msgs.append(detail)
        try:
            mark_alert_notified(history_id)
        except Exception:
            logging.exception("Error marcando alerta %s", ticker)

    if msgs:
        body = "; ".join(msgs[:3])
        if len(msgs) > 3:
            body += f" (+{len(msgs)-3} más)"
        _send_push("Alertas pendientes", body[:200], "/alertas")


def job_check_exdividend():
    """Verifica si algún ticker tiene fecha ex-dividendo en los próximos 3 días."""
    tickers = get_tickers_as_yaml_dict()
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
                    f"{name} ({ticker}): {ex_date.strftime('%d/%m/%Y')} en {days_until}d{div_str}"
                )
        except Exception:
            logging.debug("Error obteniendo ex-dividend de %s", ticker)

    if alerts:
        body = "; ".join(alerts[:3])
        if len(alerts) > 3:
            body += f" (+{len(alerts)-3} más)"
        _send_push("Próximas fechas ex-dividendo 💰", body[:200], "/")
        logging.info("Ex-dividend alert: %d ticker(s)", len(alerts))


def job_check_earnings():
    """Avisa si algún ticker tiene earnings en los próximos 7 días."""
    tickers = get_tickers_as_yaml_dict()
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
            stock = yf.Ticker(ticker)
            cal = {}
            try:
                cal_df = stock.calendar
                if isinstance(cal_df, dict):
                    cal = cal_df
            except Exception:
                pass
            earnings_date = None
            if cal.get("Earnings Date"):
                ed = cal["Earnings Date"]
                earnings_date = ed[0] if isinstance(ed, list) else ed
            elif (stock.info or {}).get("earningsDate"):
                ts = stock.info["earningsDate"]
                if isinstance(ts, (int, float)):
                    earnings_date = datetime.date.fromtimestamp(ts)
            if not earnings_date:
                continue
            if hasattr(earnings_date, "date"):
                earnings_date = earnings_date.date()
            days_until = (earnings_date - today).days
            if 0 <= days_until <= 7:
                eps_str = ""
                eps = cal.get("Earnings Average") or cal.get("EPS Estimate") or cal.get("epsAverage")
                if eps is not None:
                    try:
                        eps_str = f" EPS est: ${float(eps):.2f}"
                    except Exception:
                        pass
                alerts.append(
                    f"{name} ({ticker}): {earnings_date.strftime('%d/%m/%Y')} en {days_until}d{eps_str}"
                )
        except Exception:
            logging.debug("Error obteniendo earnings de %s", ticker)

    if alerts:
        body = "; ".join(alerts[:3])
        if len(alerts) > 3:
            body += f" (+{len(alerts)-3} más)"
        _send_push("Próximos earnings 📊", body[:200], "/")
        logging.info("Earnings alert: %d ticker(s)", len(alerts))


def job_check_sector_concentration():
    """Alerta si algún sector supera el umbral configurado (default 40%)."""
    try:
        df = get_latest_snapshot_as_df()
        if df is None:
            return
        portfolio = df[df["category"] == "portfolio"].copy()
        if portfolio.empty:
            return
        positions = {r[0]: (r[1], r[2]) for r in get_all_positions()}
        portfolio["value"] = portfolio.apply(
            lambda r: positions.get(r["ticker"], (0, 0))[0] * r.get("price", 0)
            if r.get("price") and not (isinstance(r.get("price"), float) and math.isnan(r.get("price")))
            else 0.0, axis=1
        )
        total = portfolio["value"].sum()
        if total <= 0:
            return
        threshold = 40.0
        try:
            from database import get_setting
            t_raw = get_setting("SECTOR_ALERT_THRESHOLD")
            if t_raw:
                threshold = float(t_raw)
        except Exception:
            pass
        by_sector = portfolio.groupby("block")["value"].sum()
        alerts = []
        for sector, val in by_sector.items():
            if sector and str(sector) != "nan":
                pct = val / total * 100
                if pct > threshold:
                    alerts.append(f"{sector}: {pct:.1f}% (umbral {threshold:.0f}%)")
        if alerts:
            body = "; ".join(alerts)
            _send_push("Concentración sectorial alta 🏭", body[:200], "/distribucion")
            logging.info("Sector concentration alert: %s", alerts)
    except Exception:
        logging.exception("Error en job_check_sector_concentration")


def job_vacuum_db():
    """Mantenimiento semanal de SQLite: purga datos antiguos y compacta el fichero."""
    try:
        ph = purge_old_price_history(days=365)
        nc = purge_old_news_cache(days=30)
        logging.info("Purga: %d snapshots >1 año, %d traducciones >30 días.", ph, nc)
        vacuum_db()
        logging.info("VACUUM semanal completado.")
    except Exception:
        logging.exception("Error en mantenimiento semanal de BD")


def job_check_claude_health():
    """Comprueba semanalmente que la API de Claude responde."""
    ok = check_api_health()
    if ok:
        logging.info("Claude API healthcheck OK.")
    else:
        logging.error("Claude API no responde.")
        _send_push(
            "Alerta de sistema ⚠️",
            "La API de Claude no responde. Revisa la clave API o el saldo de la cuenta.",
            "/settings/app",
        )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()

    try:
        _rh = int(effective("REPORT_HOUR", str(REPORT_HOUR)))
        if not 0 <= _rh <= 23:
            _rh = REPORT_HOUR
    except (ValueError, TypeError):
        _rh = REPORT_HOUR

    _tz_name = effective("TIMEZONE", TIMEZONE)
    try:
        tz = ZoneInfo(_tz_name)
    except Exception:
        tz = ZoneInfo(TIMEZONE)
        _tz_name = TIMEZONE

    scheduler = BlockingScheduler(timezone=tz)

    scheduler.add_job(
        job_daily_report, CronTrigger(hour=_rh, minute=0, timezone=tz),
        id="daily_report", name="Reporte diario",
    )
    scheduler.add_job(
        job_check_exdividend, CronTrigger(hour=7, minute=0, timezone=tz),
        id="exdividend", name="Check ex-dividend",
    )
    scheduler.add_job(
        job_check_earnings, CronTrigger(hour=7, minute=0, timezone=tz),
        id="earnings", name="Check earnings",
    )
    scheduler.add_job(
        job_check_price_alerts, IntervalTrigger(hours=1),
        id="price_alerts", name="Check price alerts",
    )
    scheduler.add_job(
        job_check_sector_concentration, IntervalTrigger(hours=24),
        id="sector_concentration", name="Check sector concentration",
    )
    scheduler.add_job(
        job_vacuum_db, CronTrigger(day_of_week="sun", hour=2, minute=0, timezone=tz),
        id="vacuum_db", name="Vacuum DB",
    )
    scheduler.add_job(
        job_check_claude_health, CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=tz),
        id="claude_health", name="Claude health check",
    )

    # Alertas pendientes al arrancar (30s delay para que la BD esté estable)
    run_date = datetime.datetime.now(tz) + datetime.timedelta(seconds=30)
    scheduler.add_job(
        job_replay_unnotified_alerts, "date", run_date=run_date,
        id="replay_alerts", name="Replay unnotified alerts",
    )

    logging.info("Scheduler iniciado. Reporte diario a las %d:00 %s.", _rh, _tz_name)
    scheduler.start()


if __name__ == "__main__":
    main()
