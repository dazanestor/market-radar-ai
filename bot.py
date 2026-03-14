import logging
import datetime
import io
import math
import os
from functools import wraps

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import yaml
from zoneinfo import ZoneInfo
import yfinance as yf

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from generate_csv import generate
from scoring import score_watchlist
from ai_analysis import analyze
from fetch_data import get_macro_context, get_news, to_eur, clear_fx_cache
from database import (
    init_db, save_snapshot, save_report, get_recent_reports,
    upsert_position, delete_position, get_all_positions,
    add_price_alert, get_active_alerts, deactivate_alert,
    get_ticker_history,
)
from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OUTPUT_DIR,
    REPORT_HOUR, TIMEZONE,
)

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)

DRAWDOWN_ALERT_THRESHOLD = -20
TELEGRAM_MAX_CHARS = 4096

def _fmt(price):
    return f"€{price:.2f}"


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
    """Envía texto dividiéndolo en bloques. Si un bloque falla por Markdown inválido lo reenvía como texto plano."""
    for chunk in _split_text(text):
        try:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
        except Exception:
            await bot.send_message(chat_id=chat_id, text=chunk)

async def _reply_long(update, text, parse_mode="Markdown"):
    """reply_text con soporte de mensajes largos. Fallback a texto plano si Markdown falla."""
    for chunk in _split_text(text):
        try:
            await update.message.reply_text(chunk, parse_mode=parse_mode)
        except Exception:
            await update.message.reply_text(chunk)


# ── Auth ──────────────────────────────────────────────────────────────────────

def authorized(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            await update.message.reply_text("⛔ No autorizado.")
            return
        return await func(update, context)
    return wrapper


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_tickers():
    try:
        with open("tickers.yaml") as f:
            data = yaml.safe_load(f) or {}
        return {k: (v or {}) for k, v in data.items()}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        logging.error(f"tickers.yaml inválido: {e}")
        return {}

def _save_tickers(data):
    with open("tickers.yaml", "w") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)

def _read_last_csv():
    path = f"{OUTPUT_DIR}/precios_global.csv"
    if os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception:
            return None
    return None

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

def _chart_price(ticker, hist):
    """Genera chart de precio del último año con máximo 52 semanas."""
    close = hist["Close"].tail(252)
    dates = close.index

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(dates, close.values, linewidth=1.5, color="#2196F3", label="Precio")

    high_52w = close.max()
    ax.axhline(high_52w, color="#4CAF50", linestyle="--", linewidth=1,
               label=f"Máx 52s: €{high_52w:.2f}")

    current = close.iloc[-1]
    ax.scatter([dates[-1]], [current], color="#FF5722", zorder=5, s=60, label=f"Actual: €{current:.2f}")

    ax.fill_between(dates, close.values, high_52w,
                    where=close.values < high_52w, alpha=0.08, color="#F44336")

    ax.set_title(f"{ticker} — Precio último año (€)", fontsize=13)
    ax.set_ylabel("Precio (€)")
    ax.legend(fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    fig.autofmt_xdate()
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf

def _chart_drawdown_history(ticker, rows):
    """Genera chart de evolución de drawdown desde price_history."""
    dates_raw = [r[0] for r in rows][::-1]
    drawdowns = [r[2] for r in rows][::-1]
    dates = pd.to_datetime(dates_raw)

    x = list(range(len(dates)))
    fig, ax = plt.subplots(figsize=(10, 3))
    ax.plot(x, drawdowns, color="#F44336", linewidth=1.5, marker="o", markersize=4)
    ax.axhline(0, color="#888", linewidth=0.8, linestyle="--")
    ax.fill_between(x, drawdowns, 0,
                    where=[d is not None and d < 0 for d in drawdowns], alpha=0.15, color="#F44336")
    ax.set_title(f"{ticker} — Drawdown desde máximo 52s (historial radar)", fontsize=12)
    ax.set_ylabel("Drawdown (%)")
    ax.set_xticks(x)
    ax.set_xticklabels(dates, rotation=45, fontsize=7)
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf

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

    news_by_ticker = {ticker: get_news(ticker) for ticker in df["ticker"].tolist()}

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


# ── Comandos ──────────────────────────────────────────────────────────────────

@authorized
async def cmd_ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*Market Radar AI*\n\n"
        "📊 *Análisis*\n"
        "/reporte — análisis completo con IA ahora\n"
        "/cartera — posiciones del portfolio\n"
        "/watchlist — oportunidades en watchlist\n"
        "/rebalanceo — pesos actuales vs objetivos\n"
        "/grafico `<ticker>` — chart de precio último año\n"
        "/fundamentos `<ticker>` — PER, P/B, ROE, margen, deuda y noticias\n"
        "/historial `<ticker>` — evolución de drawdown\n"
        "/reportes — últimos análisis de Claude\n\n"
        "💼 *Posiciones*\n"
        "/posicion `<ticker> <acciones> <precio>` — registra posición\n"
        "/eliminar\\_posicion `<ticker>` — elimina posición\n\n"
        "🔔 *Alertas de precio*\n"
        "/alerta `<ticker> <precio>` — alerta cuando cruza ese precio\n"
        "/mis\\_alertas — ver alertas activas\n"
        "/borrar\\_alerta `<id>` — elimina alerta\n\n"
        "⚙️ *Configuración*\n"
        "/agregar `<cat> <ticker> <nombre> <bloque> <region>` — añade ticker\n"
        "/eliminar `<ticker>` — elimina ticker del radar\n"
        "/ayuda — este mensaje\n\n"
        "_Reporte automático cada día a las {hour}:00 {tz}._"
    ).format(hour=REPORT_HOUR, tz=TIMEZONE)
    await update.message.reply_text(text, parse_mode="Markdown")

@authorized
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando análisis, espera un momento...")
    await _run_report(context.bot, update.effective_chat.id)

@authorized
async def cmd_cartera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = _read_last_csv()
    if df is None:
        await update.message.reply_text("Sin datos aún. Ejecuta /reporte primero.")
        return

    portfolio = df[df["category"] == "portfolio"]
    if portfolio.empty:
        await update.message.reply_text("No hay activos en el portfolio.")
        return

    positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}
    lines = ["*CARTERA*\n"]

    for _, row in portfolio.iterrows():
        ticker = row["ticker"]
        trend_icon = "📉" if row.get("trend") == "empeorando" else "📈" if row.get("trend") == "mejorando" else "➡️"
        mom = f"{row['momentum_3m']:.1f}%" if pd.notna(row.get("momentum_3m")) else "—"
        vol = f"{row['volatility']:.1f}%" if pd.notna(row.get("volatility")) else "—"
        line = (
            f"{trend_icon} *{ticker}* — {row['name']}\n"
            f"  Precio: {_fmt(row['price'])}   Drawdown: {row['drawdown_52w']:.1f}%\n"
            f"  Mom 3m: {mom}   Volatilidad: {vol}"
        )
        if ticker in positions:
            shares, avg = positions[ticker]
            line += f"\n  Posición: {shares} acc @ {_fmt(avg)}"
            if avg:
                pnl = (row["price"] - avg) / avg * 100
                line += f"   P&L: {pnl:+.1f}%"
        lines.append(line)

    await _reply_long(update, "\n\n".join(lines))

@authorized
async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = _read_last_csv()
    if df is None:
        await update.message.reply_text("Sin datos aún. Ejecuta /reporte primero.")
        return

    watchlist = df[df["category"] == "watchlist"].copy()
    if watchlist.empty:
        await update.message.reply_text("No hay activos en watchlist.")
        return

    watchlist = score_watchlist(watchlist).sort_values("score", ascending=False)
    lines = ["*WATCHLIST*\n"]

    for _, row in watchlist.iterrows():
        opp = row.get("opportunity", "—")
        emoji = "🟢" if opp == "ALTA" else "🟡" if opp == "MEDIA" else "🔴"
        mom = f"{row['momentum_3m']:.1f}%" if pd.notna(row.get("momentum_3m")) else "—"
        line = (
            f"{emoji} *{row['ticker']}* — {row['name']}  `{opp}`\n"
            f"  Precio: {_fmt(row['price'])}   Drawdown: {row['drawdown_52w']:.1f}%\n"
            f"  Mom 3m: {mom}   Dividendo: {row['dividend_yield']:.1f}%   Score: {row['score']}"
        )
        lines.append(line)

    await _reply_long(update, "\n\n".join(lines))

@authorized
async def cmd_rebalanceo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    df = _read_last_csv()
    if df is None:
        await update.message.reply_text("Sin datos aún. Ejecuta /reporte primero.")
        return

    portfolio = df[df["category"] == "portfolio"]
    positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}

    if not positions:
        await update.message.reply_text(
            "No hay posiciones registradas.\nUsa /posicion `<ticker> <acciones> <precio>` para añadirlas.",
            parse_mode="Markdown"
        )
        return

    rows_data = []
    for _, row in portfolio.iterrows():
        ticker = row["ticker"]
        if ticker in positions:
            shares, avg = positions[ticker]
            value = shares * row["price"]
            rows_data.append({
                "ticker": ticker,
                "name": row["name"],
                "value": value,
                "target_weight": row.get("target_weight"),
            })

    if not rows_data:
        await update.message.reply_text("No hay posiciones que coincidan con el portfolio.")
        return

    total = sum(r["value"] for r in rows_data)
    if not total:
        await update.message.reply_text("El valor total de la cartera es 0. Revisa los precios o las posiciones.")
        return
    lines = [f"*REBALANCEO* (valor total: {_fmt(total)})\n"]

    for r in sorted(rows_data, key=lambda x: -x["value"]):
        current_w = r["value"] / total * 100
        target_w = r["target_weight"]

        try:
            target_w_float = float(target_w) if pd.notna(target_w) and target_w else None
        except (ValueError, TypeError):
            target_w_float = None

        if target_w_float:
            diff = current_w - target_w_float
            action = "✅ OK" if abs(diff) < 2 else ("✂️ RECORTAR" if diff > 0 else "➕ AÑADIR")
            weight_line = f"Actual: {current_w:.1f}%   Objetivo: {target_w_float}%   {action}"
        else:
            weight_line = f"Actual: {current_w:.1f}%   (sin objetivo definido)"

        lines.append(f"*{r['ticker']}* — {r['name']}\n  Valor: {_fmt(r['value'])}   {weight_line}")

    await _reply_long(update, "\n\n".join(lines))

@authorized
async def cmd_grafico(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /grafico `<ticker>`", parse_mode="Markdown")
        return

    ticker = context.args[0].upper()
    await update.message.reply_text(f"⏳ Generando gráfico de *{ticker}*...", parse_mode="Markdown")

    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1y")
        if hist.empty:
            await update.message.reply_text(f"❌ No se encontraron datos para *{ticker}*.", parse_mode="Markdown")
            return
        try:
            currency = (t.info or {}).get("currency", "USD")
        except Exception:
            currency = "USD"
        hist = hist.copy()
        hist["Close"] = hist["Close"].apply(lambda p: to_eur(p, currency))
        buf = _chart_price(ticker, hist)
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=buf)
    except Exception as e:
        await update.message.reply_text(f"❌ Error al generar gráfico: {e}")

@authorized
async def cmd_fundamentos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /fundamentos `<ticker>`", parse_mode="Markdown")
        return

    ticker = context.args[0].upper()
    await update.message.reply_text(f"⏳ Obteniendo datos de *{ticker}*...", parse_mode="Markdown")

    try:
        info = yf.Ticker(ticker).info or {}
        news = get_news(ticker, n=5, translate=True)

        def fmt(v, suffix=""):
            if v is None:
                return "—"
            try:
                if math.isnan(float(v)):
                    return "—"
            except (TypeError, ValueError):
                pass
            return f"{v}{suffix}"

        pe = info.get("trailingPE")
        pb = info.get("priceToBook")
        margin = info.get("profitMargins")
        roe = info.get("returnOnEquity")
        de = info.get("debtToEquity")
        rev_growth = info.get("revenueGrowth")
        market_cap = info.get("marketCap")
        currency = info.get("currency", "USD")
        def _sanitize(s):
            return s.replace("*", "").replace("_", " ").replace("`", "").replace("[", "").replace("]", "")

        name = _sanitize(info.get("longName", ticker))
        sector = _sanitize(info.get("sector", "—"))
        description = info.get("longBusinessSummary", "")

        cap_eur = to_eur(market_cap, currency) if market_cap else None
        cap_str = f"€{cap_eur/1e9:.1f}B" if cap_eur else "—"
        raw_desc = (description[:300] + "...") if len(description) > 300 else description
        desc_str = raw_desc.replace("*", "").replace("_", " ").replace("`", "").replace("[", "").replace("]", "")

        lines = [
            f"*{ticker}* — {name}",
            f"Sector: {sector}   Cap: {cap_str}\n",
            "*Valoración*",
            f"  PER: {fmt(round(pe,1) if pe is not None else None)}   P/B: {fmt(round(pb,2) if pb is not None else None)}",
            "",
            "*Rentabilidad*",
            f"  Margen neto: {fmt(round(margin*100,1) if margin is not None else None, '%')}   ROE: {fmt(round(roe*100,1) if roe is not None else None, '%')}",
            f"  Crec. ingresos: {fmt(round(rev_growth*100,1) if rev_growth is not None else None, '%')}",
            "",
            "*Deuda*",
            f"  D/E: {fmt(round(de,2) if de is not None else None)}",
        ]

        if desc_str:
            lines += ["", "*Descripción*", desc_str]

        if news:
            lines += ["", "*Noticias recientes*"] + news

        await _reply_long(update, "\n".join(lines))

    except Exception as e:
        await update.message.reply_text(f"❌ Error al obtener datos de *{ticker}*: {e}", parse_mode="Markdown")

@authorized
async def cmd_historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /historial `<ticker>`", parse_mode="Markdown")
        return

    ticker = context.args[0].upper()
    rows = get_ticker_history(ticker, days=30)

    if not rows:
        await update.message.reply_text(
            f"Sin historial para *{ticker}*. Los datos se acumulan con cada ejecución del radar.",
            parse_mode="Markdown"
        )
        return

    # Text summary
    lines = [f"*HISTORIAL {ticker}* (últimos {len(rows)} días)\n"]
    for date, price, drawdown, score, opp in rows[:10]:
        opp_icon = "🟢" if opp == "ALTA" else "🟡" if opp == "MEDIA" else "🔴"
        score_str = f"{score}" if score is not None else "—"
        price_str = _fmt(price) if price is not None else "—"
        dd_str = f"{drawdown:.1f}%" if drawdown is not None else "—"
        lines.append(f"`{date}`  {price_str}  DD: {dd_str}  Score: {score_str}  {opp_icon}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # Chart if enough data
    if len(rows) >= 3:
        buf = _chart_drawdown_history(ticker, rows)
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=buf)

@authorized
async def cmd_reportes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reports = get_recent_reports(n=5)
    if not reports:
        await update.message.reply_text("Sin reportes guardados aún. Ejecuta /reporte primero.")
        return

    for report_id, report_date, content in reports:
        header = f"📋 *Reporte #{report_id}* — {report_date}\n\n"
        await _reply_long(update, header + content)

@authorized
async def cmd_posicion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 3:
        await update.message.reply_text("Uso: /posicion `<ticker> <acciones> <precio_medio>`", parse_mode="Markdown")
        return
    try:
        ticker = context.args[0].upper()
        shares = float(context.args[1])
        avg_price = float(context.args[2])
        if shares <= 0 or avg_price <= 0:
            await update.message.reply_text("❌ Acciones y precio deben ser mayores que 0.")
            return
        upsert_position(ticker, shares, avg_price)

        # Añadir automáticamente al radar si no está ya
        tickers = _load_tickers()
        added_to_radar = False
        if ticker not in tickers.get("portfolio", {}) and ticker not in tickers.get("watchlist", {}):
            if "portfolio" not in tickers:
                tickers["portfolio"] = {}
            try:
                info = yf.Ticker(ticker).info or {}
                name = info.get("longName") or info.get("shortName") or ticker
                block = info.get("sector") or "—"
                region = info.get("country") or "—"
            except Exception:
                name, block, region = ticker, "—", "—"
            tickers["portfolio"][ticker] = {"name": name, "block": block, "region": region}
            _save_tickers(tickers)
            added_to_radar = True

        msg = f"✅ Posición guardada: *{ticker}* — {shares} acc @ {_fmt(avg_price)}"
        if added_to_radar:
            msg += f"\n📡 Añadido al radar: *{name}* | {block} | {region}"
        await update.message.reply_text(msg, parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Acciones y precio deben ser números.")

@authorized
async def cmd_eliminar_posicion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /eliminar\\_posicion `<ticker>`", parse_mode="Markdown")
        return
    ticker = context.args[0].upper()
    delete_position(ticker)
    await update.message.reply_text(f"🗑️ Posición de *{ticker}* eliminada.", parse_mode="Markdown")

@authorized
async def cmd_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text(
            "Uso: /alerta `<ticker> <precio>`\n"
            "Si el precio objetivo es menor al actual → alerta al bajar.\n"
            "Si es mayor → alerta al subir.",
            parse_mode="Markdown"
        )
        return
    try:
        ticker = context.args[0].upper()
        target = float(context.args[1])

        df = _read_last_csv()
        direction = None
        current_str = ""
        current = None

        if df is not None:
            row = df[df["ticker"] == ticker]
            if not row.empty:
                current = row.iloc[0]["price"]

        if current is None:
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period="2d")
                if not hist.empty:
                    cur = (t.info or {}).get("currency", "USD")
                    current = to_eur(hist["Close"].iloc[-1], cur)
            except Exception:
                pass

        if current is not None:
            direction = "below" if target < current else "above"
            current_str = f" (actual: {_fmt(current)})"
        else:
            direction = "below" if target < 0 else "above"

        add_price_alert(ticker, target, direction)
        dir_text = "baje a" if direction == "below" else "suba a"
        await update.message.reply_text(
            f"🔔 Alerta creada: avísame cuando *{ticker}* {dir_text} {_fmt(target)}{current_str}.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("❌ El precio debe ser un número.")

@authorized
async def cmd_mis_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    alerts = get_active_alerts()
    if not alerts:
        await update.message.reply_text("No tienes alertas activas.")
        return

    lines = ["*ALERTAS ACTIVAS*\n"]
    for alert_id, ticker, target, direction, created in alerts:
        icon = "📉" if direction == "below" else "📈"
        dir_text = "baje a" if direction == "below" else "suba a"
        lines.append(f"`#{alert_id}` {icon} *{ticker}* — {dir_text} {_fmt(target)}  _{created}_")

    lines.append("\nUsa /borrar\\_alerta `<id>` para eliminar.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

@authorized
async def cmd_borrar_alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /borrar\\_alerta `<id>`", parse_mode="Markdown")
        return
    try:
        alert_id = int(context.args[0])
        deactivate_alert(alert_id)
        await update.message.reply_text(f"🗑️ Alerta #{alert_id} eliminada.")
    except ValueError:
        await update.message.reply_text("❌ El ID debe ser un número.")

@authorized
async def cmd_agregar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 5:
        await update.message.reply_text(
            "Uso: /agregar `<categoria> <ticker> <nombre> <bloque> <region>`\n"
            "Ejemplo: `/agregar watchlist AAPL Apple Tecnología USA`",
            parse_mode="Markdown"
        )
        return

    category = context.args[0].lower()
    if category not in ("portfolio", "watchlist"):
        await update.message.reply_text("❌ Categoría debe ser `portfolio` o `watchlist`.", parse_mode="Markdown")
        return

    ticker = context.args[1].upper()
    region = context.args[-1]
    block = context.args[-2]
    name = " ".join(context.args[2:-2]) if len(context.args) > 5 else context.args[2]

    tickers = _load_tickers()
    if category not in tickers:
        tickers[category] = {}

    tickers[category][ticker] = {"name": name, "block": block, "region": region}
    _save_tickers(tickers)

    await update.message.reply_text(
        f"✅ *{ticker}* ({name}) añadido a *{category}*.\nBloque: {block} | Región: {region}",
        parse_mode="Markdown"
    )

@authorized
async def cmd_eliminar_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 1:
        await update.message.reply_text("Uso: /eliminar `<ticker>`", parse_mode="Markdown")
        return

    ticker = context.args[0].upper()
    tickers = _load_tickers()
    found = False

    for category in tickers:
        if ticker in tickers[category]:
            del tickers[category][ticker]
            found = True
            break

    if found:
        _save_tickers(tickers)
        await update.message.reply_text(f"🗑️ *{ticker}* eliminado del radar.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ *{ticker}* no encontrado.", parse_mode="Markdown")


# ── Jobs periódicos ───────────────────────────────────────────────────────────

async def job_daily_report(context: ContextTypes.DEFAULT_TYPE):
    logging.info("Ejecutando reporte diario...")
    await _run_report(context.bot, TELEGRAM_CHAT_ID)

async def job_check_price_alerts(context: ContextTypes.DEFAULT_TYPE):
    alerts = get_active_alerts()
    if not alerts:
        return

    tickers_needed = list({a[1] for a in alerts})
    prices = {}
    clear_fx_cache()
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

    for alert_id, ticker, target, direction, _ in alerts:
        current = prices.get(ticker)
        if current is None or (isinstance(current, float) and math.isnan(current)):
            continue

        triggered = (direction == "below" and current <= target) or \
                    (direction == "above" and current >= target)

        if triggered:
            icon = "📉" if direction == "below" else "📈"
            msg = (
                f"{icon} *Alerta disparada*\n"
                f"*{ticker}* ha {'bajado a' if direction == 'below' else 'subido a'} "
                f"{_fmt(current)} (objetivo: {_fmt(target)})"
            )
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown"
            )
            deactivate_alert(alert_id)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_ayuda))
    app.add_handler(CommandHandler("ayuda", cmd_ayuda))
    app.add_handler(CommandHandler("reporte", cmd_reporte))
    app.add_handler(CommandHandler("cartera", cmd_cartera))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("rebalanceo", cmd_rebalanceo))
    app.add_handler(CommandHandler("grafico", cmd_grafico))
    app.add_handler(CommandHandler("fundamentos", cmd_fundamentos))
    app.add_handler(CommandHandler("historial", cmd_historial))
    app.add_handler(CommandHandler("reportes", cmd_reportes))
    app.add_handler(CommandHandler("posicion", cmd_posicion))
    app.add_handler(CommandHandler("eliminar_posicion", cmd_eliminar_posicion))
    app.add_handler(CommandHandler("alerta", cmd_alerta))
    app.add_handler(CommandHandler("mis_alertas", cmd_mis_alertas))
    app.add_handler(CommandHandler("borrar_alerta", cmd_borrar_alerta))
    app.add_handler(CommandHandler("agregar", cmd_agregar))
    app.add_handler(CommandHandler("eliminar", cmd_eliminar_ticker))

    tz = ZoneInfo(TIMEZONE)
    report_time = datetime.time(hour=REPORT_HOUR, minute=0, tzinfo=tz)
    app.job_queue.run_daily(job_daily_report, time=report_time)
    app.job_queue.run_repeating(job_check_price_alerts, interval=3600, first=60)

    logging.info(f"Bot iniciado. Reporte diario a las {REPORT_HOUR}:00 {TIMEZONE}.")
    app.run_polling()


if __name__ == "__main__":
    main()
