"""
Interfaz web para Market Radar AI.
Uso: uvicorn web:app --host 0.0.0.0 --port 8589
     python web.py
"""
import asyncio
import io
import json
import math
import os
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import yaml
import yfinance as yf

from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from ai_analysis import analyze
from config import OUTPUT_DIR
from database import (
    add_price_alert,
    deactivate_alert,
    delete_position,
    get_active_alerts,
    get_all_positions,
    get_recent_reports,
    get_ticker_history,
    get_tr_cache,
    init_db,
    save_report,
    save_snapshot,
    set_tr_cache,
    upsert_position,
)
from fetch_data import get_macro_context, get_news
from generate_csv import generate
from scoring import score_watchlist

# ── Config ────────────────────────────────────────────────────────────────────

WEB_PASSWORD  = os.getenv("WEB_PASSWORD", "")
SESSION_TOKEN = secrets.token_hex(32)

app       = FastAPI(title="Market Radar AI", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory="templates")
_executor = ThreadPoolExecutor(max_workers=4)

# ── Chart constants (dark theme) ──────────────────────────────────────────────

_C_BG    = "#161b22"
_C_CARD  = "#21262d"
_C_GRID  = "#30363d"
_C_TEXT  = "#8b949e"
_C_FG    = "#e6edf3"
_C_BLUE  = "#58a6ff"
_C_GREEN = "#3fb950"
_C_RED   = "#f85149"


# ── Jinja2 filters ────────────────────────────────────────────────────────────

def _is_nan(v) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def fmt_eur(v) -> str:
    if v is None or _is_nan(v):
        return "—"
    return f"€{float(v):,.2f}"


def fmt_pct(v) -> str:
    if v is None or _is_nan(v):
        return "—"
    return f"{float(v):+.1f}%"


def fmt_num(v) -> str:
    if v is None or _is_nan(v):
        return "—"
    return f"{float(v):.2f}"


def dd_class(v) -> str:
    try:
        v = float(v)
        if _is_nan(v):  return "text-muted"
        if v < -20:     return "text-negative"
        if v < -10:     return "text-warning"
        if v < -5:      return "text-muted"
        return "text-positive"
    except (TypeError, ValueError):
        return "text-muted"


def pnl_class(v) -> str:
    try:
        v = float(v)
        if _is_nan(v): return "text-muted"
        return "text-positive" if v >= 0 else "text-negative"
    except (TypeError, ValueError):
        return "text-muted"


def opp_class(v) -> str:
    if v == "ALTA":  return "badge badge-green"
    if v == "MEDIA": return "badge badge-yellow"
    return "badge badge-red"


def tg_to_html(text) -> Markup:
    if not text:
        return Markup("")
    text = str(escape(text))
    text = re.sub(r'`([^`\n]+)`',    r'<code class="tg-code">\1</code>', text)
    text = re.sub(r'\*([^\*\n]+)\*', r'<strong>\1</strong>', text)
    text = re.sub(r'_([^_\n]+)_',    r'<em>\1</em>', text)
    text = text.replace('\n', '<br>')
    return Markup(text)


templates.env.filters.update({
    "eur":       fmt_eur,
    "pct":       fmt_pct,
    "num":       fmt_num,
    "dd_class":  dd_class,
    "pnl_class": pnl_class,
    "opp_class": opp_class,
    "tg":        tg_to_html,
})


# ── Auth ──────────────────────────────────────────────────────────────────────

def _is_auth(session: Optional[str]) -> bool:
    return not WEB_PASSWORD or session == SESSION_TOKEN


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_tickers() -> dict:
    try:
        with open("tickers.yaml") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _save_tickers(data: dict):
    with open("tickers.yaml", "w") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


def _read_csv() -> Optional[pd.DataFrame]:
    path = f"{OUTPUT_DIR}/precios_global.csv"
    if os.path.exists(path):
        try:
            return pd.read_csv(path)
        except Exception:
            pass
    return None


def _safe_pct(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        return None if math.isnan(f) else round(f * 100, 1)
    except (TypeError, ValueError):
        return None


def _tr_status() -> str:
    """
    Retorna el estado del módulo Trade Republic:
      not_configured — faltan TR_PHONE / TR_PIN en el entorno
      needs_setup    — credenciales OK pero keyfile no existe
      setup_pending  — setup iniciado, esperando código SMS
      ready          — keyfile presente, listo para sincronizar
    """
    try:
        from trade_republic import is_configured, is_setup, has_pending_setup
        if not is_configured():
            return "not_configured"
        if has_pending_setup():
            return "setup_pending"
        if is_setup():
            return "ready"
        return "needs_setup"
    except ImportError:
        return "not_configured"


def _do_generate_report():
    macro = get_macro_context()
    df, _ = generate()
    if df.empty:
        return
    df = score_watchlist(df)
    save_snapshot(df.to_dict("records"))
    portfolio_df = df[df["category"] == "portfolio"].copy()
    watchlist_df = df[df["category"] == "watchlist"].copy()
    news_by_ticker = {t: get_news(t) for t in df["ticker"].tolist()}
    ai_report = analyze(portfolio_df, watchlist_df, macro=macro, news_by_ticker=news_by_ticker)
    save_report(ai_report)


# ── Chart generation (dark theme) ─────────────────────────────────────────────

def _style_ax(ax, fig):
    fig.patch.set_facecolor(_C_BG)
    ax.set_facecolor(_C_BG)
    ax.tick_params(colors=_C_TEXT, labelsize=8)
    ax.yaxis.label.set_color(_C_TEXT)
    ax.xaxis.label.set_color(_C_TEXT)
    ax.title.set_color(_C_FG)
    for spine in ax.spines.values():
        spine.set_color(_C_GRID)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(alpha=0.25, color=_C_GRID, linewidth=0.5)


def _fig_to_response(fig) -> Response:
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=110, facecolor=_C_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return Response(content=buf.read(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


def _make_price_chart(ticker: str, hist: pd.DataFrame) -> Optional[plt.Figure]:
    close = hist["Close"].tail(252).dropna()
    if close.empty:
        return None
    dates = close.index
    high_52w = close.max()
    current = close.iloc[-1]

    fig, ax = plt.subplots(figsize=(9, 3.8))
    _style_ax(ax, fig)

    ax.fill_between(dates, close.values, high_52w,
                    where=close.values < high_52w, alpha=0.07, color=_C_RED)
    ax.plot(dates, close.values, linewidth=1.5, color=_C_BLUE)
    ax.axhline(high_52w, color=_C_GREEN, linestyle="--", linewidth=0.9,
               label=f"Máx 52s: {high_52w:.2f}")
    ax.scatter([dates[-1]], [current], color=_C_RED, zorder=5, s=55,
               label=f"Actual: {current:.2f}")

    ax.set_title(f"{ticker}  —  Precio último año", fontsize=12, pad=10)
    ax.legend(fontsize=8, facecolor=_C_CARD, edgecolor=_C_GRID, labelcolor=_C_FG,
              framealpha=0.9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def _make_history_chart(ticker: str, rows: list) -> Optional[plt.Figure]:
    dates_raw  = [r[0] for r in rows][::-1]
    drawdowns  = [r[2] for r in rows][::-1]
    dates = pd.to_datetime(dates_raw)

    fig, ax = plt.subplots(figsize=(9, 2.8))
    _style_ax(ax, fig)

    ax.plot(dates, drawdowns, color=_C_RED, linewidth=1.5, marker="o", markersize=3.5)
    ax.axhline(0, color=_C_TEXT, linewidth=0.7, linestyle="--")
    ax.fill_between(dates, drawdowns, 0,
                    where=[d is not None and d < 0 for d in drawdowns],
                    alpha=0.12, color=_C_RED)
    ax.set_title(f"{ticker}  —  Drawdown desde máx 52s (historial radar)", fontsize=11, pad=8)
    ax.set_ylabel("Drawdown (%)", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=40)
    fig.tight_layout()
    return fig


# ── Chart endpoints ───────────────────────────────────────────────────────────

@app.get("/chart/precio/{ticker}")
async def chart_precio(ticker: str, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)
    ticker = ticker.upper()

    def _fetch():
        return yf.Ticker(ticker).history(period="1y")

    hist = await asyncio.get_running_loop().run_in_executor(_executor, _fetch)
    if hist.empty:
        raise HTTPException(404, "Sin datos")
    fig = _make_price_chart(ticker, hist)
    if fig is None:
        raise HTTPException(404, "Sin datos")
    return _fig_to_response(fig)


@app.get("/chart/historial/{ticker}")
async def chart_historial(ticker: str, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)
    rows = get_ticker_history(ticker.upper(), days=30)
    if len(rows) < 2:
        raise HTTPException(404, "Sin historial suficiente")
    fig = _make_history_chart(ticker.upper(), rows)
    if fig is None:
        raise HTTPException(404)
    return _fig_to_response(fig)


# ── Login ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login(password: str = Form(...)):
    if WEB_PASSWORD and password != WEB_PASSWORD:
        return RedirectResponse("/login?error=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", SESSION_TOKEN, httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df = _read_csv()
    portfolio, watchlist = [], []
    total_value = 0.0

    if df is not None:
        df_s      = score_watchlist(df)
        positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}

        for _, row in df_s[df_s["category"] == "portfolio"].iterrows():
            d     = row.to_dict()
            price = d.get("price")
            pnl   = value = shares = avg = None
            if d["ticker"] in positions:
                shares, avg = positions[d["ticker"]]
                if price and not _is_nan(price):
                    value        = shares * price
                    total_value += value
                if avg and price and not _is_nan(price):
                    pnl = (price - avg) / avg * 100
            d["shares"]   = shares
            d["avg_price"] = avg
            d["value"]    = value
            d["pnl_pct"]  = pnl
            portfolio.append(d)

        for _, row in df_s[df_s["category"] == "watchlist"] \
                          .sort_values("score", ascending=False).iterrows():
            watchlist.append(row.to_dict())

    reports = get_recent_reports(n=1)

    tr_cash_row = get_tr_cache("cash_eur")
    tr_cash = float(tr_cash_row[0]) if tr_cash_row else None

    return templates.TemplateResponse("dashboard.html", {
        "request":     request,
        "portfolio":   portfolio,
        "watchlist":   watchlist,
        "total_value": total_value if total_value else None,
        "last_report": reports[0] if reports else None,
        "has_data":    df is not None,
        "n_alerts":    len(get_active_alerts()),
        "n_tickers":   len(portfolio) + len(watchlist),
        "tr_cash":     tr_cash,
    })


@app.post("/generar-reporte")
async def generar_reporte(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    await asyncio.get_running_loop().run_in_executor(_executor, _do_generate_report)
    return RedirectResponse("/", status_code=303)


# ── Rebalanceo ────────────────────────────────────────────────────────────────

@app.get("/rebalanceo", response_class=HTMLResponse)
async def rebalanceo_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df        = _read_csv()
    positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}
    rows_data = []

    if df is not None:
        for _, row in df[df["category"] == "portfolio"].iterrows():
            ticker = row["ticker"]
            if ticker not in positions:
                continue
            shares, avg = positions[ticker]
            price = row.get("price")
            if not price or _is_nan(price):
                continue
            value = shares * float(price)
            try:
                tw = float(row.get("target_weight")) if row.get("target_weight") and not _is_nan(row.get("target_weight", float("nan"))) else None
            except (TypeError, ValueError):
                tw = None
            rows_data.append({
                "ticker": ticker,
                "name":   row["name"],
                "shares": shares,
                "price":  float(price),
                "value":  value,
                "target_w": tw,
            })

    total = sum(r["value"] for r in rows_data) if rows_data else 0.0

    for r in rows_data:
        r["current_w"] = r["value"] / total * 100 if total else 0.0
        if r["target_w"]:
            diff = r["current_w"] - r["target_w"]
            r["diff"] = diff
            if abs(diff) < 2:
                r["action"] = ("ok",      "✅ OK",       "badge-green")
            elif diff > 0:
                r["action"] = ("reduce",  "✂️ Recortar", "badge-red")
            else:
                r["action"] = ("add",     "➕ Añadir",   "badge-blue")
        else:
            r["diff"]   = None
            r["action"] = ("none", "—", "badge-gray")

    rows_data.sort(key=lambda x: -x["value"])

    return templates.TemplateResponse("rebalanceo.html", {
        "request":       request,
        "rows":          rows_data,
        "total":         total,
        "has_positions": bool(positions),
        "has_data":      df is not None,
    })


# ── Noticias ──────────────────────────────────────────────────────────────────

@app.get("/noticias", response_class=HTMLResponse)
async def noticias_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    tickers_yaml = _load_tickers()
    ticker_list  = []
    for cat in ("portfolio", "watchlist"):
        for ticker, meta in (tickers_yaml.get(cat) or {}).items():
            name = meta.get("name", ticker) if isinstance(meta, dict) else ticker
            ticker_list.append((ticker, name, cat))

    def _fetch_all():
        result = []
        for ticker, name, cat in ticker_list:
            items = get_news(ticker, n=5, translate=True)
            if items:
                result.append({"ticker": ticker, "name": name, "category": cat, "items": items})
        return result

    news_data = await asyncio.get_running_loop().run_in_executor(_executor, _fetch_all)
    portfolio_news  = [n for n in news_data if n["category"] == "portfolio"]
    watchlist_news  = [n for n in news_data if n["category"] == "watchlist"]

    return templates.TemplateResponse("noticias.html", {
        "request":        request,
        "portfolio_news": portfolio_news,
        "watchlist_news": watchlist_news,
        "total_tickers":  len(ticker_list),
    })


# ── Ticker detalle ────────────────────────────────────────────────────────────

@app.get("/ticker/{ticker}", response_class=HTMLResponse)
async def ticker_detalle(ticker: str, request: Request,
                         session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    ticker = ticker.upper()

    def _fetch():
        info = {}
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            pass
        news = get_news(ticker, n=8, translate=True)
        return info, news

    info, news = await asyncio.get_running_loop().run_in_executor(_executor, _fetch)

    rows        = get_ticker_history(ticker, days=30)
    has_history = len(rows) >= 2

    df      = _read_csv()
    csv_row = None
    if df is not None:
        r = df[df["ticker"] == ticker]
        if not r.empty:
            csv_row = r.iloc[0].to_dict()

    market_cap = info.get("marketCap")
    cap_b      = round(market_cap / 1e9, 1) if market_cap and not _is_nan(market_cap) else None

    description = info.get("longBusinessSummary", "")
    if len(description) > 500:
        description = description[:500] + "…"

    fundamentals = {
        "name":           info.get("longName") or info.get("shortName") or ticker,
        "sector":         info.get("sector", "—"),
        "country":        info.get("country", "—"),
        "cap_b":          cap_b,
        "currency":       info.get("currency", "—"),
        "pe_ratio":       info.get("trailingPE"),
        "pb_ratio":       info.get("priceToBook"),
        "profit_margin":  _safe_pct(info.get("profitMargins")),
        "roe":            _safe_pct(info.get("returnOnEquity")),
        "debt_equity":    info.get("debtToEquity"),
        "revenue_growth": _safe_pct(info.get("revenueGrowth")),
        "description":    description,
    }

    return templates.TemplateResponse("ticker_detalle.html", {
        "request":      request,
        "ticker":       ticker,
        "fundamentals": fundamentals,
        "news":         news,
        "csv_row":      csv_row,
        "has_history":  has_history,
        "hist_rows":    rows[:10],
    })


# ── Tickers ───────────────────────────────────────────────────────────────────

@app.get("/tickers", response_class=HTMLResponse)
async def tickers_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    tickers = _load_tickers()
    return templates.TemplateResponse("tickers.html", {
        "request":   request,
        "portfolio": tickers.get("portfolio", {}),
        "watchlist": tickers.get("watchlist", {}),
    })


@app.post("/tickers/add")
async def tickers_add(
    session:       Optional[str] = Cookie(default=None),
    categoria:     str = Form(...),
    ticker:        str = Form(...),
    nombre:        str = Form(...),
    bloque:        str = Form(...),
    region:        str = Form(...),
    target_weight: str = Form(""),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    tickers = _load_tickers()
    if categoria not in tickers:
        tickers[categoria] = {}
    entry: dict = {"name": nombre, "block": bloque, "region": region}
    if target_weight:
        try:
            entry["target_weight"] = float(target_weight)
        except ValueError:
            pass
    tickers[categoria][ticker.strip().upper()] = entry
    _save_tickers(tickers)
    return RedirectResponse("/tickers", status_code=303)


@app.post("/tickers/delete")
async def tickers_delete(
    session:   Optional[str] = Cookie(default=None),
    ticker:    str = Form(...),
    categoria: str = Form(...),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    tickers = _load_tickers()
    if categoria in tickers and ticker in tickers[categoria]:
        del tickers[categoria][ticker]
    _save_tickers(tickers)
    return RedirectResponse("/tickers", status_code=303)


# ── Posiciones ────────────────────────────────────────────────────────────────

@app.get("/posiciones", response_class=HTMLResponse)
async def posiciones_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    df           = _read_csv()
    tickers_yaml = _load_tickers()
    # Construir mapa ticker → nombre desde tickers.yaml (portfolio + watchlist)
    yaml_names = {}
    for cat in ("portfolio", "watchlist"):
        for t, meta in (tickers_yaml.get(cat) or {}).items():
            if isinstance(meta, dict) and meta.get("name"):
                yaml_names[t] = meta["name"]

    pos_data = []
    for ticker, shares, avg_price in get_all_positions():
        price = pnl = value = name = None
        if df is not None:
            row = df[df["ticker"] == ticker]
            if not row.empty:
                r     = row.iloc[0]
                price = r.get("price")
                name  = r.get("name")
                if price and not _is_nan(price) and avg_price:
                    pnl   = (price - avg_price) / avg_price * 100
                    value = shares * price
        # Fallback: nombre desde tickers.yaml
        if not name or name == ticker:
            name = yaml_names.get(ticker, ticker)
        pos_data.append({
            "ticker": ticker, "name": name, "shares": shares,
            "avg_price": avg_price, "price": price,
            "pnl": pnl, "value": value,
        })
    tr_cash_row  = get_tr_cache("cash_eur")
    tr_unmatched_row = get_tr_cache("tr_unmatched")
    tr_unmatched = []
    if tr_unmatched_row:
        try:
            tr_unmatched = json.loads(tr_unmatched_row[0])
        except Exception:
            pass

    return templates.TemplateResponse("posiciones.html", {
        "request":      request,
        "positions":    pos_data,
        "tr_status":    _tr_status(),
        "tr_cash":      float(tr_cash_row[0]) if tr_cash_row else None,
        "tr_unmatched": tr_unmatched,
    })


@app.post("/posiciones/add")
async def posiciones_add(
    session:   Optional[str] = Cookie(default=None),
    ticker:    str   = Form(...),
    shares:    float = Form(...),
    avg_price: float = Form(...),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    if shares > 0 and avg_price > 0:
        upsert_position(ticker.strip().upper(), shares, avg_price)
    return RedirectResponse("/posiciones", status_code=303)


@app.post("/posiciones/delete")
async def posiciones_delete(
    session: Optional[str] = Cookie(default=None),
    ticker:  str = Form(...),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    delete_position(ticker)
    return RedirectResponse("/posiciones", status_code=303)


# ── Alertas ───────────────────────────────────────────────────────────────────

@app.get("/alertas", response_class=HTMLResponse)
async def alertas_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    df = _read_csv()
    tickers_disponibles = []
    if df is not None:
        for _, r in df.sort_values("ticker").iterrows():
            tickers_disponibles.append({"ticker": r["ticker"], "name": r.get("name", r["ticker"])})
    return templates.TemplateResponse("alertas.html", {
        "request":             request,
        "alerts":              get_active_alerts(),
        "tickers_disponibles": tickers_disponibles,
    })


@app.post("/alertas/add")
async def alertas_add(
    session:      Optional[str] = Cookie(default=None),
    ticker:       str   = Form(...),
    target_price: float = Form(...),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    df        = _read_csv()
    direction = "below"
    if df is not None:
        row = df[df["ticker"] == ticker.upper()]
        if not row.empty:
            current = row.iloc[0].get("price")
            if current and not _is_nan(current):
                direction = "below" if target_price < current else "above"
    add_price_alert(ticker.strip().upper(), target_price, direction)
    return RedirectResponse("/alertas", status_code=303)


@app.post("/alertas/delete")
async def alertas_delete(
    session:  Optional[str] = Cookie(default=None),
    alert_id: int = Form(...),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    deactivate_alert(alert_id)
    return RedirectResponse("/alertas", status_code=303)


# ── Trade Republic ────────────────────────────────────────────────────────────

@app.post("/tr/setup/start")
async def tr_setup_start(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    def _start():
        from trade_republic import setup_device
        return setup_device()

    try:
        await asyncio.get_running_loop().run_in_executor(_executor, _start)
        return RedirectResponse("/posiciones?tr_msg=sms_sent", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/posiciones?tr_error={e}", status_code=303)


@app.post("/tr/setup/complete")
async def tr_setup_complete(
    session: Optional[str] = Cookie(default=None),
    code:    str = Form(...),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    def _complete():
        from trade_republic import complete_setup
        return complete_setup(code.strip())

    try:
        await asyncio.get_running_loop().run_in_executor(_executor, _complete)
        return RedirectResponse("/posiciones?tr_msg=setup_ok", status_code=303)
    except Exception as e:
        return RedirectResponse(f"/posiciones?tr_error={e}", status_code=303)


@app.post("/tr/sync")
async def tr_sync(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    def _do_sync():
        from trade_republic import sync_positions
        return sync_positions()

    try:
        positions, cash_eur, transactions = await asyncio.get_running_loop().run_in_executor(_executor, _do_sync)
    except Exception as e:
        return RedirectResponse(f"/posiciones?tr_error={e}", status_code=303)

    synced    = 0
    unmatched = []
    tickers_data = _load_tickers()
    portfolio_yaml = tickers_data.setdefault("portfolio", {})
    isin_map_yaml  = tickers_data.setdefault("tr_isin_map", {})
    yaml_changed   = False

    # Detectar tickers mal resueltos comparando el país del ISIN con el sufijo del ticker
    from trade_republic import _ISIN_COUNTRY_TO_EXCH, _OPENFIGI_SUFFIX, _OPENFIGI_US
    stale_isins = []
    for isin, ticker in isin_map_yaml.items():
        if not ticker:
            continue
        country = isin[:2]
        preferred_exch = _ISIN_COUNTRY_TO_EXCH.get(country)
        if not preferred_exch:
            continue  # País desconocido o US, no validar
        expected_suffix = _OPENFIGI_SUFFIX[preferred_exch]
        if not ticker.endswith(expected_suffix):
            stale_isins.append(isin)
    for isin in stale_isins:
        bad_ticker = isin_map_yaml.pop(isin)
        portfolio_yaml.pop(bad_ticker, None)
        delete_position(bad_ticker)
        yaml_changed = True
        print(f"TR: ticker {bad_ticker} no coincide con país del ISIN {isin}, se re-resolverá")

    for pos in positions:
        if pos.get("matched") and pos.get("ticker"):
            ticker = pos["ticker"]
            upsert_position(ticker, pos["shares"], pos["avg_price"])
            synced += 1
            # Añadir al portfolio del yaml si no existe
            if ticker not in portfolio_yaml:
                portfolio_yaml[ticker] = {"name": pos.get("name", ticker)}
                yaml_changed = True
        else:
            unmatched.append(pos)

    # Intentar resolver ISINs sin mapear via OpenFIGI
    if unmatched:
        from trade_republic import resolve_isins_openfigi
        unmatched_isins = [p["isin"] for p in unmatched]
        resolved = await asyncio.get_running_loop().run_in_executor(
            _executor, resolve_isins_openfigi, unmatched_isins
        )
        still_unmatched = []
        for pos in unmatched:
            isin   = pos["isin"]
            ticker = resolved.get(isin)
            if ticker:
                isin_map_yaml[isin] = ticker
                portfolio_yaml.setdefault(ticker, {"name": pos.get("name", ticker)})
                upsert_position(ticker, pos["shares"], pos["avg_price"])
                synced += 1
                yaml_changed = True
            else:
                still_unmatched.append({"isin": isin, "name": pos["name"],
                                        "shares": pos["shares"], "avg_price": pos["avg_price"]})
                if isin not in isin_map_yaml:
                    isin_map_yaml[isin] = None
                    yaml_changed = True
        unmatched = still_unmatched

    if yaml_changed:
        _save_tickers(tickers_data)

    if cash_eur is not None:
        set_tr_cache("cash_eur", str(cash_eur))
    set_tr_cache("tr_unmatched", json.dumps(unmatched))
    if transactions:
        set_tr_cache("tr_transactions", json.dumps(transactions))

    return RedirectResponse(
        f"/posiciones?tr_synced={synced}&tr_unmatched={len(unmatched)}",
        status_code=303,
    )


@app.get("/tr/historial", response_class=HTMLResponse)
async def tr_historial_page(
    request:  Request,
    session:  Optional[str] = Cookie(default=None),
    tf:       str = "1y",
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    timeframes = ["1d", "1w", "1m", "3m", "6m", "1y", "max"]
    if tf not in timeframes:
        tf = "1y"

    try:
        from trade_republic import is_configured, is_setup
        tr_ready = is_configured() and is_setup()
    except ImportError:
        tr_ready = False

    # Transacciones cacheadas del último sync
    tx_row = get_tr_cache("tr_transactions")
    transactions = []
    if tx_row:
        try:
            transactions = json.loads(tx_row[0])
        except Exception:
            pass

    return templates.TemplateResponse("tr_historial.html", {
        "request":      request,
        "tr_ready":     tr_ready,
        "timeframe":    tf,
        "timeframes":   timeframes,
        "transactions": transactions,
    })


def _make_tr_history_chart(items: list, timeframe: str):
    """Genera el gráfico de valor del depósito TR en el tiempo."""
    import datetime as dt
    times, values = [], []
    for item in items:
        try:
            times.append(dt.datetime.fromtimestamp(item["time_ms"] / 1000, tz=dt.timezone.utc))
            values.append(item["value"])
        except (KeyError, TypeError, ValueError):
            pass

    if len(values) < 2:
        return None

    fig, ax = plt.subplots(figsize=(9, 3.8))
    _style_ax(ax, fig)

    ax.fill_between(times, values, min(values), alpha=0.08, color=_C_GREEN)
    ax.plot(times, values, linewidth=1.5, color=_C_GREEN)
    ax.scatter([times[-1]], [values[-1]], color=_C_BLUE, zorder=5, s=55,
               label=f"Actual: €{values[-1]:,.2f}")

    perf = (values[-1] / values[0] - 1) * 100 if values[0] else 0
    color_perf = _C_GREEN if perf >= 0 else _C_RED
    ax.set_title(
        f"Depósito Trade Republic  —  {timeframe}  ({perf:+.1f}%)",
        fontsize=12, pad=10, color=color_perf,
    )
    ax.legend(fontsize=8, facecolor=_C_CARD, edgecolor=_C_GRID, labelcolor=_C_FG, framealpha=0.9)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"€{v:,.0f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y" if timeframe not in ("1d", "1w") else "%d %b"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


@app.get("/chart/tr/historial/{timeframe}")
async def chart_tr_historial(
    timeframe: str,
    session:   Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        raise HTTPException(401)

    valid = {"1d", "1w", "1m", "3m", "6m", "1y", "max"}
    if timeframe not in valid:
        raise HTTPException(400, "timeframe inválido")

    def _fetch_and_draw():
        from trade_republic import get_portfolio_history
        items = get_portfolio_history(timeframe)
        return _make_tr_history_chart(items, timeframe)

    try:
        fig = await asyncio.get_running_loop().run_in_executor(_executor, _fetch_and_draw)
    except Exception as e:
        raise HTTPException(503, f"Error TR: {e}")

    if fig is None:
        raise HTTPException(404, "Sin datos")
    return _fig_to_response(fig)


# ── Reportes ──────────────────────────────────────────────────────────────────

@app.get("/reportes", response_class=HTMLResponse)
async def reportes_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("reportes.html", {
        "request": request,
        "reports": get_recent_reports(n=10),
    })


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8589")))
