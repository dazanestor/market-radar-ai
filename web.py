"""
Interfaz web para Market Radar AI.
Uso: uvicorn web:app --host 0.0.0.0 --port 8589
     python web.py
"""
import asyncio
import csv
import io
import json
import logging
import math
import os
import re
import secrets
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import bcrypt
import pyotp
import segno

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import yaml
import yfinance as yf

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from ai_analysis import analyze
from config import OUTPUT_DIR
from database import (
    add_price_alert,
    count_reports,
    deactivate_alert,
    delete_position,
    get_active_alerts,
    get_alert_history,
    get_all_positions,
    get_recent_reports,
    get_ticker_history,
    get_tr_cache,
    init_db,
    log_alert_triggered,
    save_report,
    save_snapshot,
    set_tr_cache,
    upsert_position,
)
from fetch_data import get_macro_context, get_news
from generate_csv import generate
from scoring import score_watchlist

logger = logging.getLogger("web")

# ── Config ────────────────────────────────────────────────────────────────────

CREDENTIALS_FILE   = "data/credentials.json"
TOTP_SECRET_FILE   = "data/totp_secret.key"
DEFAULT_USERNAME   = "admin"

# Sesiones activas: session_id → expiry timestamp (monotonic)
SESSION_EXPIRY    = 86400 * 30  # 30 días
_active_sessions: dict = {}

# CSRF: rota cada 24h; acepta token anterior durante 1h post-rotación
_CSRF_ROTATION = 86400
_CSRF_OVERLAP  = 3600
_csrf_state: dict = {
    "current":    secrets.token_urlsafe(32),
    "previous":   None,
    "rotated_at": _time.monotonic(),
}

# Tokens temporales en memoria: token → timestamp de expiración
_pending_tokens: dict = {}

# Lockout de login: ip → lista de timestamps de fallos recientes
_LOCKOUT_MAX      = 5
_LOCKOUT_DURATION = 900  # 15 minutos (en segundos)
_failed_logins: dict = {}

# Caché del CSV en memoria
_csv_cache: dict = {"df": None, "ts": 0.0}
_CSV_CACHE_TTL   = 300.0  # 5 minutos


# ── Credential helpers ────────────────────────────────────────────────────────

_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_\-\.]{3,32}$')


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        # Timing-safe: consumir tiempo similar al de un checkpw real
        bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(4))
        return False


def _validate_password(password: str) -> Optional[str]:
    if len(password) < 10:
        return "La contraseña debe tener al menos 10 caracteres."
    if not re.search(r'[A-Za-z]', password):
        return "La contraseña debe contener al menos una letra."
    if not re.search(r'[0-9]', password):
        return "La contraseña debe contener al menos un número."
    return None


def _load_credentials() -> dict:
    try:
        with open(CREDENTIALS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        initial_password = secrets.token_urlsafe(12)
        creds = {
            "username": DEFAULT_USERNAME,
            "password_hash": _hash_password(initial_password),
            "first_login": True,
        }
        os.makedirs("data", exist_ok=True)
        with open(CREDENTIALS_FILE, "w") as f:
            json.dump(creds, f)
        os.chmod(CREDENTIALS_FILE, 0o600)
        logger.warning(
            "\n" + "=" * 52 +
            "\n  PRIMER ARRANQUE — Credenciales iniciales" +
            f"\n  Usuario:     {DEFAULT_USERNAME}" +
            f"\n  Contraseña:  {initial_password}" +
            "\n  Cambia estas credenciales en el asistente de\n  primer acceso antes de usar la aplicación." +
            "\n" + "=" * 52
        )
        return creds


def _save_credentials(username: str, password: str, first_login: bool = False) -> None:
    creds = {
        "username": username,
        "password_hash": _hash_password(password),
        "first_login": first_login,
    }
    os.makedirs("data", exist_ok=True)
    tmp = CREDENTIALS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(creds, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDENTIALS_FILE)


# ── Pending tokens ────────────────────────────────────────────────────────────

def _create_pending_token(max_age: int) -> str:
    token = secrets.token_urlsafe(32)
    _pending_tokens[token] = _time.monotonic() + max_age
    return token


def _consume_pending_token(token: Optional[str]) -> bool:
    if not token:
        return False
    expiry = _pending_tokens.pop(token, None)
    return expiry is not None and _time.monotonic() < expiry


# ── TOTP helpers ──────────────────────────────────────────────────────────────

def _totp_secret() -> Optional[str]:
    try:
        s = open(TOTP_SECRET_FILE).read().strip()
        return s or None
    except FileNotFoundError:
        return None


def _totp_enabled() -> bool:
    return bool(_totp_secret())


def _verify_totp(code: str) -> bool:
    secret = _totp_secret()
    if not secret:
        return True
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=2)


limiter      = Limiter(key_func=get_remote_address)
app          = FastAPI(title="Market Radar AI", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
templates    = Jinja2Templates(directory="templates")
_executor    = ThreadPoolExecutor(max_workers=4)
_chart_lock  = threading.Lock()  # matplotlib no es thread-safe

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
# CSRF token disponible en todos los templates como {{ csrf_token }}
templates.env.globals["csrf_token"] = _csrf_state["current"]


@app.middleware("http")
async def _refresh_csrf_global(request: Request, call_next):
    """Rota el CSRF token si ha expirado; añade Cache-Control en respuestas HTML."""
    _rotate_csrf_if_needed()
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ── Auth ──────────────────────────────────────────────────────────────────────

def _create_session() -> str:
    sid = secrets.token_urlsafe(32)
    _active_sessions[sid] = _time.monotonic() + SESSION_EXPIRY
    return sid

def _is_auth(session: Optional[str]) -> bool:
    if not session or session not in _active_sessions:
        return False
    if _time.monotonic() > _active_sessions[session]:
        del _active_sessions[session]
        return False
    return True

def _invalidate_session(session: Optional[str]) -> None:
    if session:
        _active_sessions.pop(session, None)

def _check_lockout(ip: str) -> bool:
    """Devuelve True si la IP está bloqueada por exceso de fallos."""
    now = _time.monotonic()
    attempts = [t for t in _failed_logins.get(ip, []) if now - t < _LOCKOUT_DURATION]
    _failed_logins[ip] = attempts
    return len(attempts) >= _LOCKOUT_MAX

def _record_failed_login(ip: str) -> None:
    _failed_logins.setdefault(ip, []).append(_time.monotonic())

def _reset_lockout(ip: str) -> None:
    _failed_logins.pop(ip, None)

def _rotate_csrf_if_needed() -> None:
    """Rota el CSRF token cada 24h y actualiza el global de Jinja2."""
    now = _time.monotonic()
    if now - _csrf_state["rotated_at"] >= _CSRF_ROTATION:
        _csrf_state["previous"]   = _csrf_state["current"]
        _csrf_state["current"]    = secrets.token_urlsafe(32)
        _csrf_state["rotated_at"] = now
        templates.env.globals["csrf_token"] = _csrf_state["current"]


def _validate_csrf(token: Optional[str]) -> bool:
    if not token:
        return False
    if secrets.compare_digest(token, _csrf_state["current"]):
        return True
    prev = _csrf_state["previous"]
    if prev:
        since_rotation = _time.monotonic() - _csrf_state["rotated_at"]
        if since_rotation < _CSRF_OVERLAP and secrets.compare_digest(token, prev):
            return True
    return False


def _require_csrf(request: Request, token: Optional[str]) -> None:
    """Valida el token CSRF o lanza 403 con log de la IP."""
    if not _validate_csrf(token):
        logger.warning("CSRF inválido: ip=%s path=%s", request.client.host, request.url.path)
        raise HTTPException(403, "Token CSRF inválido")


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


_SECTOR_TO_BLOCK = {
    "Technology":             "Tecnología",
    "Financial Services":     "Financiero",
    "Healthcare":             "Salud",
    "Consumer Defensive":     "Consumo básico",
    "Consumer Cyclical":      "Consumo cíclico",
    "Communication Services": "Comunicaciones",
    "Industrials":            "Industrial",
    "Basic Materials":        "Materiales",
    "Energy":                 "Energía",
    "Real Estate":            "Inmobiliario",
    "Utilities":              "Utilities",
}
_COUNTRY_TO_REGION = {
    "United States": "USA",
    "Switzerland":   "Europa",
    "Denmark":       "Europa",
    "United Kingdom":"Europa",
    "France":        "Europa",
    "Germany":       "Europa",
    "Netherlands":   "Europa",
    "Sweden":        "Europa",
    "Spain":         "Europa",
    "Italy":         "Europa",
    "Belgium":       "Europa",
    "Finland":       "Europa",
    "Norway":        "Europa",
    "Portugal":      "Europa",
    "Australia":     "Asia-Pacífico",
    "Japan":         "Asia-Pacífico",
    "China":         "Asia-Pacífico",
    "Hong Kong":     "Asia-Pacífico",
    "South Korea":   "Asia-Pacífico",
    "India":         "Asia-Pacífico",
    "Canada":        "América",
    "Brazil":        "América",
}


def _enrich_ticker_meta(ticker: str, meta: dict) -> dict:
    """Rellena block y region en meta usando yfinance si están vacíos."""
    if meta.get("block") and meta.get("region"):
        return meta
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        if not meta.get("block"):
            sector = info.get("sector", "")
            meta["block"] = _SECTOR_TO_BLOCK.get(sector, sector or None)
        if not meta.get("region"):
            country = info.get("country", "")
            meta["region"] = _COUNTRY_TO_REGION.get(country, country or None)
    except Exception:
        logger.exception("Error enriqueciendo meta de %s", ticker)
    return meta


def _read_csv() -> Optional[pd.DataFrame]:
    now = _time.monotonic()
    if _csv_cache["df"] is not None and now - _csv_cache["ts"] < _CSV_CACHE_TTL:
        return _csv_cache["df"]
    path = f"{OUTPUT_DIR}/precios_global.csv"
    if os.path.exists(path):
        try:
            df = pd.read_csv(path)
            _csv_cache["df"] = df
            _csv_cache["ts"] = now
            return df
        except Exception:
            logger.exception("Error leyendo CSV de precios")
    return None

def _invalidate_csv_cache() -> None:
    _csv_cache["df"] = None
    _csv_cache["ts"] = 0.0


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
    from concurrent.futures import as_completed as _as_completed
    ticker_list = df["ticker"].tolist()
    news_futures = {_executor.submit(get_news, t): t for t in ticker_list}
    news_by_ticker = {}
    for fut in _as_completed(news_futures, timeout=120):
        t = news_futures[fut]
        try:
            news_by_ticker[t] = fut.result()
        except Exception:
            news_by_ticker[t] = []
    ai_report = analyze(portfolio_df, watchlist_df, macro=macro, news_by_ticker=news_by_ticker)
    save_report(ai_report)
    _invalidate_csv_cache()


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
    with _chart_lock:
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
        try:
            return yf.Ticker(ticker).history(period="1y")
        except Exception:
            logger.exception("Error obteniendo historial de %s", ticker)
            return pd.DataFrame()

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


# ── QR code ───────────────────────────────────────────────────────────────────

def _make_qr_svg(uri: str) -> str:
    buf = io.BytesIO()
    segno.make_qr(uri).save(buf, kind="svg", scale=4, border=1, xmldecl=False, nl=False)
    return buf.getvalue().decode("utf-8")



# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    import datetime as _dt
    from fastapi.responses import JSONResponse
    status = "ok"
    try:
        from database import _db
        with _db() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        status = f"db_error: {exc}"
    csv_path = f"{OUTPUT_DIR}/precios_global.csv"
    return JSONResponse({
        "status":     status,
        "csv_exists": os.path.exists(csv_path),
        "timestamp":  _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    })


# ── Login ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    ip = get_remote_address(request)
    if _check_lockout(ip):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "locked",
        }, status_code=429)

    creds = _load_credentials()
    if username != creds["username"] or not _verify_password(password, creds["password_hash"]):
        _record_failed_login(ip)
        return RedirectResponse("/login?error=1", status_code=303)

    _reset_lockout(ip)

    # Primer login: forzar configuración
    if creds.get("first_login"):
        token = _create_pending_token(600)
        resp = RedirectResponse("/setup/first-login", status_code=303)
        resp.set_cookie("setup_pending", token, httponly=True, samesite="strict", max_age=600)
        return resp

    # 2FA activo
    if _totp_enabled():
        token = _create_pending_token(300)
        resp = RedirectResponse("/login/totp", status_code=303)
        resp.set_cookie("totp_pending", token, httponly=True, samesite="strict", max_age=300)
        return resp

    sid = _create_session()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", sid, httponly=True, samesite="strict", max_age=SESSION_EXPIRY)
    return resp


@app.get("/login/totp", response_class=HTMLResponse)
async def totp_page(request: Request, totp_pending: Optional[str] = Cookie(default=None), error: str = ""):
    if not totp_pending or totp_pending not in _pending_tokens:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("login_totp.html", {"request": request, "error": error})


@app.post("/login/totp")
@limiter.limit("5/minute")
async def totp_verify(
    request: Request,
    code: str = Form(...),
    totp_pending: Optional[str] = Cookie(default=None),
):
    if not _consume_pending_token(totp_pending):
        return RedirectResponse("/login", status_code=303)
    if not _verify_totp(code):
        new_token = _create_pending_token(300)
        resp = templates.TemplateResponse(
            "login_totp.html", {"request": request, "error": "1"}, status_code=200
        )
        resp.set_cookie("totp_pending", new_token, httponly=True, samesite="strict", max_age=300)
        return resp
    sid = _create_session()
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", sid, httponly=True, samesite="strict", max_age=SESSION_EXPIRY)
    resp.delete_cookie("totp_pending")
    return resp


# ── Primer login ───────────────────────────────────────────────────────────────

@app.get("/setup/first-login", response_class=HTMLResponse)
async def first_login_page(request: Request, setup_pending: Optional[str] = Cookie(default=None)):
    if not setup_pending or setup_pending not in _pending_tokens:
        return RedirectResponse("/login", status_code=302)
    totp_secret = pyotp.random_base32()
    totp_uri = pyotp.TOTP(totp_secret).provisioning_uri(name="Market Radar AI", issuer_name="MarketRadar")
    return templates.TemplateResponse("setup_first_login.html", {
        "request": request,
        "totp_secret": totp_secret,
        "totp_uri": totp_uri,
        "qr_svg": _make_qr_svg(totp_uri),
        "error": "",
    })


@app.post("/setup/first-login")
@limiter.limit("5/minute")
async def first_login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    totp_secret: str = Form(...),
    totp_code: str = Form(...),
    setup_pending: Optional[str] = Cookie(default=None),
):
    if not _consume_pending_token(setup_pending):
        return RedirectResponse("/login", status_code=303)

    errors = []
    if not _USERNAME_RE.match(username.strip()):
        errors.append("Usuario: solo letras, números, guión, punto o guión bajo (3-32 caracteres).")
    pwd_err = _validate_password(password)
    if pwd_err:
        errors.append(pwd_err)
    if password != password2:
        errors.append("Las contraseñas no coinciden.")
    if not pyotp.TOTP(totp_secret).verify(totp_code.strip(), valid_window=2):
        errors.append("Código 2FA incorrecto. Vuelve a escanear el QR e inténtalo.")

    if errors:
        totp_uri = pyotp.TOTP(totp_secret).provisioning_uri(name="Market Radar AI", issuer_name="MarketRadar")
        # Reissue token para que pueda volver a enviar el formulario
        new_token = _create_pending_token(600)
        resp = templates.TemplateResponse("setup_first_login.html", {
            "request": request,
            "totp_secret": totp_secret,
            "totp_uri": totp_uri,
            "qr_svg": _make_qr_svg(totp_uri),
            "error": " ".join(errors),
            "form_username": username,
        })
        resp.set_cookie("setup_pending", new_token, httponly=True, samesite="strict", max_age=600)
        return resp

    _save_credentials(username.strip(), password, first_login=False)
    os.makedirs("data", exist_ok=True)
    with open(TOTP_SECRET_FILE, "w") as f:
        f.write(totp_secret)
    os.chmod(TOTP_SECRET_FILE, 0o600)

    resp = RedirectResponse("/login?setup_ok=1", status_code=303)
    resp.delete_cookie("setup_pending")
    return resp


# ── 2FA (usuario ya autenticado) ───────────────────────────────────────────────

@app.get("/2fa/setup", response_class=HTMLResponse)
async def totp_setup_page(request: Request, session: Optional[str] = Cookie(default=None), ok: str = "", disabled: str = ""):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    enabled = _totp_enabled()
    secret = _totp_secret() if enabled else pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name="Market Radar AI", issuer_name="MarketRadar")
    return templates.TemplateResponse("2fa_setup.html", {
        "request": request,
        "enabled": enabled,
        "secret": secret,
        "uri": uri,
        "qr_svg": _make_qr_svg(uri) if not enabled else "",
        "ok": ok,
        "disabled": disabled,
    })


@app.post("/2fa/setup")
async def totp_setup(
    request: Request,
    code: str = Form(...),
    secret: str = Form(...),
    session: Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    totp = pyotp.TOTP(secret)
    if not totp.verify(code.strip(), valid_window=2):
        uri = totp.provisioning_uri(name="Market Radar AI", issuer_name="MarketRadar")
        return templates.TemplateResponse("2fa_setup.html", {
            "request": request, "enabled": False, "secret": secret,
            "uri": uri, "qr_svg": _make_qr_svg(uri), "ok": "", "disabled": "",
            "error": "Código incorrecto. Vuelve a escanear el QR e inténtalo.",
        })
    os.makedirs("data", exist_ok=True)
    with open(TOTP_SECRET_FILE, "w") as f:
        f.write(secret)
    os.chmod(TOTP_SECRET_FILE, 0o600)
    return RedirectResponse("/2fa/setup?ok=1", status_code=303)


@app.post("/2fa/disable")
async def totp_disable(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    try:
        os.remove(TOTP_SECRET_FILE)
    except FileNotFoundError:
        pass
    return RedirectResponse("/2fa/setup?disabled=1", status_code=303)


# ── Cambiar credenciales ───────────────────────────────────────────────────────

@app.get("/settings/credentials", response_class=HTMLResponse)
async def credentials_page(request: Request, session: Optional[str] = Cookie(default=None), ok: str = ""):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    creds = _load_credentials()
    return templates.TemplateResponse("settings_credentials.html", {
        "request": request,
        "current_username": creds["username"],
        "ok": ok,
    })


@app.post("/settings/credentials")
async def credentials_update(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    session: Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    creds = _load_credentials()
    errors = []
    if not _USERNAME_RE.match(username.strip()):
        errors.append("Usuario: solo letras, números, guión, punto o guión bajo (3-32 caracteres).")
    pwd_err = _validate_password(password)
    if pwd_err:
        errors.append(pwd_err)
    if password != password2:
        errors.append("Las contraseñas no coinciden.")
    if errors:
        return templates.TemplateResponse("settings_credentials.html", {
            "request": request,
            "current_username": creds["username"],
            "ok": "",
            "error": " ".join(errors),
        })
    _save_credentials(username.strip(), password, first_login=False)
    return RedirectResponse("/settings/credentials?ok=1", status_code=303)


@app.get("/logout")
async def logout(session: Optional[str] = Cookie(default=None)):
    _invalidate_session(session)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    resp.delete_cookie("totp_pending")
    resp.delete_cookie("setup_pending")
    return resp


# ── Dashboard ─────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    error: Optional[str] = None,
    session: Optional[str] = Cookie(default=None),
):
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
    if tr_cash and total_value is not None:
        total_value += tr_cash

    # Antigüedad de datos: fecha de modificación del CSV
    csv_path = f"{OUTPUT_DIR}/precios_global.csv"
    data_age = None
    if os.path.exists(csv_path):
        import datetime as _dt
        mtime = os.path.getmtime(csv_path)
        age_sec = _time.time() - mtime
        if age_sec < 3600:
            data_age = f"hace {int(age_sec // 60)} min"
        elif age_sec < 86400:
            data_age = f"hace {int(age_sec // 3600)} h"
        else:
            data_age = _dt.datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")

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
        "data_age":    data_age,
        "error":       error,
    })


@app.post("/generar-reporte")
@limiter.limit("2/minute")
async def generar_reporte(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    try:
        await asyncio.get_running_loop().run_in_executor(_executor, _do_generate_report)
    except Exception:
        logger.exception("Error en /generar-reporte")
        return RedirectResponse("/?error=reporte_fallido", status_code=303)
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

    import datetime as _dt
    fetch_start = _dt.datetime.now()
    news_data = await asyncio.get_running_loop().run_in_executor(_executor, _fetch_all)
    portfolio_news  = [n for n in news_data if n["category"] == "portfolio"]
    watchlist_news  = [n for n in news_data if n["category"] == "watchlist"]

    return templates.TemplateResponse("noticias.html", {
        "request":        request,
        "portfolio_news": portfolio_news,
        "watchlist_news": watchlist_news,
        "total_tickers":  len(ticker_list),
        "fetched_at":     fetch_start.strftime("%H:%M"),
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

@app.get("/tickers/search")
async def tickers_search(q: str = "", session: Optional[str] = Cookie(default=None)):
    from fastapi.responses import JSONResponse
    if not _is_auth(session):
        return JSONResponse([])
    if len(q) < 2:
        return JSONResponse([])
    def _do_search():
        import yfinance as yf
        try:
            search  = yf.Search(q, max_results=10)
            quotes  = search.quotes
            # quotes puede ser lista de dicts o DataFrame
            if hasattr(quotes, "to_dict"):
                quotes = quotes.to_dict("records")
            results = []
            for item in (quotes or []):
                if not isinstance(item, dict):
                    continue
                symbol = item.get("symbol") or item.get("Symbol", "")
                name   = (item.get("longname") or item.get("shortname")
                          or item.get("longName") or item.get("shortName", symbol))
                type_  = item.get("typeDisp") or item.get("quoteType", "")
                exch   = item.get("exchDisp") or item.get("exchange", "")
                if not symbol:
                    continue
                # Excluir solo derivados y divisas
                if type_ in ("Currency", "Future", "Option"):
                    continue
                results.append({
                    "ticker":   symbol,
                    "name":     name,
                    "type":     type_,
                    "exchange": exch,
                })
            return results
        except Exception:
            logger.exception("Error en búsqueda de tickers")
            return []
    results = await asyncio.get_running_loop().run_in_executor(_executor, _do_search)
    return JSONResponse(results)


@app.get("/tickers/info")
async def tickers_info(ticker: str = "", session: Optional[str] = Cookie(default=None)):
    from fastapi.responses import JSONResponse
    if not _is_auth(session) or not ticker:
        return JSONResponse({})
    def _do_info():
        import yfinance as yf
        try:
            info = yf.Ticker(ticker).info or {}
            sector  = info.get("sector", "")
            country = info.get("country", "")
            return {
                "name":   info.get("longName") or info.get("shortName", ticker),
                "block":  _SECTOR_TO_BLOCK.get(sector, sector),
                "region": _COUNTRY_TO_REGION.get(country, country),
            }
        except Exception:
            return {}
    result = await asyncio.get_running_loop().run_in_executor(_executor, _do_info)
    return JSONResponse(result)


@app.get("/tickers", response_class=HTMLResponse)
async def tickers_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    tickers = _load_tickers()
    tickers_with_position = {row[0] for row in get_all_positions()}
    return templates.TemplateResponse("tickers.html", {
        "request":               request,
        "portfolio":             tickers.get("portfolio", {}),
        "watchlist":             tickers.get("watchlist", {}),
        "tickers_with_position": tickers_with_position,
    })


@app.post("/tickers/add")
async def tickers_add(
    request:       Request,
    session:       Optional[str] = Cookie(default=None),
    categoria:     str = Form(...),
    ticker:        str = Form(...),
    nombre:        str = Form(...),
    bloque:        str = Form(...),
    region:        str = Form(...),
    target_weight: str = Form(""),
    csrf_token:    Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
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


@app.post("/tickers/update")
async def tickers_update(
    request:       Request,
    session:       Optional[str] = Cookie(default=None),
    ticker:        str = Form(...),
    categoria:     str = Form(...),
    nombre:        str = Form(""),
    bloque:        str = Form(""),
    region:        str = Form(""),
    target_weight: str = Form(""),
    csrf_token:    Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    tickers = _load_tickers()
    meta = (tickers.get(categoria) or {}).get(ticker, {}) or {}
    if nombre:        meta["name"]          = nombre
    if bloque:        meta["block"]         = bloque
    if region:        meta["region"]        = region
    if target_weight: meta["target_weight"] = float(target_weight)
    tickers.setdefault(categoria, {})[ticker] = meta
    _save_tickers(tickers)
    return RedirectResponse("/tickers", status_code=303)


@app.post("/tickers/delete")
async def tickers_delete(
    request:    Request,
    session:   Optional[str] = Cookie(default=None),
    ticker:    str = Form(...),
    categoria: str = Form(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    tickers = _load_tickers()
    if categoria in tickers and ticker in tickers[categoria]:
        del tickers[categoria][ticker]
    _save_tickers(tickers)
    return RedirectResponse("/tickers", status_code=303)


# ── Posiciones ────────────────────────────────────────────────────────────────

@app.get("/posiciones", response_class=HTMLResponse)
async def posiciones_page(
    request: Request,
    saved: Optional[str] = None,
    error: Optional[str] = None,
    session: Optional[str] = Cookie(default=None),
):
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
        # Pre-calcular pnl_eur y detectar posible split (>85% pérdida)
        pnl_eur = (price - avg_price) * shares if (
            price and not _is_nan(price) and avg_price
        ) else None
        split_warning = (
            price and avg_price and not _is_nan(price)
            and price > 0 and avg_price > 0
            and (price / avg_price) < 0.15
        )
        pos_data.append({
            "ticker": ticker, "name": name, "shares": shares,
            "avg_price": avg_price, "price": price,
            "pnl": pnl, "value": value,
            "pnl_eur": pnl_eur, "split_warning": split_warning,
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
        "saved":        saved,
        "error":        error,
    })


@app.post("/posiciones/add")
async def posiciones_add(
    request:    Request,
    session:   Optional[str] = Cookie(default=None),
    ticker:    str   = Form(...),
    shares:    float = Form(...),
    avg_price: float = Form(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    t = ticker.strip().upper()
    if 0 < shares < 1_000_000 and 0 < avg_price < 1_000_000:
        upsert_position(t, shares, avg_price)
        return RedirectResponse(f"/posiciones?saved={t}", status_code=303)
    return RedirectResponse("/posiciones?error=datos_invalidos", status_code=303)


@app.post("/posiciones/delete")
async def posiciones_delete(
    request:    Request,
    session: Optional[str] = Cookie(default=None),
    ticker:  str = Form(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
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
        "alert_history":       get_alert_history(limit=30),
        "tickers_disponibles": tickers_disponibles,
    })


@app.post("/alertas/add")
async def alertas_add(
    request:        Request,
    session:        Optional[str] = Cookie(default=None),
    ticker:         str   = Form(...),
    condition_type: str   = Form("price"),
    target_price:   float = Form(...),
    csrf_token:     Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    if not (0 < target_price < 1_000_000):
        return RedirectResponse("/alertas", status_code=303)
    t = ticker.strip().upper()

    if condition_type in ("drawdown", "score"):
        # Para alertas de drawdown/score, guardamos target_price como condition_value
        add_price_alert(t, target_price, "above", condition_type=condition_type,
                        condition_value=target_price)
    else:
        df = _read_csv()
        direction = "below"
        if df is not None:
            row = df[df["ticker"] == t]
            if not row.empty:
                current = row.iloc[0].get("price")
                if current and not _is_nan(current):
                    direction = "below" if target_price < current else "above"
        add_price_alert(t, target_price, direction, condition_type="price")
    return RedirectResponse("/alertas", status_code=303)


@app.post("/alertas/delete")
async def alertas_delete(
    request:  Request,
    session:  Optional[str] = Cookie(default=None),
    alert_id: int = Form(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    deactivate_alert(alert_id)
    return RedirectResponse("/alertas", status_code=303)


# ── Trade Republic ────────────────────────────────────────────────────────────

@app.post("/tr/setup/start")
async def tr_setup_start(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)

    def _start():
        from trade_republic import setup_device
        return setup_device()

    try:
        await asyncio.get_running_loop().run_in_executor(_executor, _start)
        return RedirectResponse("/posiciones?tr_msg=sms_sent", status_code=303)
    except Exception:
        logger.exception("TR setup/start error")
        return RedirectResponse("/posiciones?tr_error=Error+en+Trade+Republic", status_code=303)


@app.post("/tr/setup/complete")
async def tr_setup_complete(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    code:    str = Form(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)

    def _complete():
        from trade_republic import complete_setup
        return complete_setup(code.strip())

    try:
        await asyncio.get_running_loop().run_in_executor(_executor, _complete)
        return RedirectResponse("/posiciones?tr_msg=setup_ok", status_code=303)
    except Exception:
        logger.exception("TR setup/complete error")
        return RedirectResponse("/posiciones?tr_error=Error+en+Trade+Republic", status_code=303)


@app.post("/tr/sync")
async def tr_sync(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)

    def _do_sync():
        from trade_republic import sync_positions
        return sync_positions()

    try:
        positions, cash_eur, transactions = await asyncio.get_running_loop().run_in_executor(_executor, _do_sync)
    except Exception as e:
        # Si la sesión expiró, re-vincular automáticamente y pedir el código
        if "expirada" in str(e).lower() or "expirado" in str(e).lower() or "no válida" in str(e).lower():
            try:
                def _relink():
                    from trade_republic import setup_device
                    return setup_device()
                await asyncio.get_running_loop().run_in_executor(_executor, _relink)
                return RedirectResponse("/posiciones?tr_msg=sms_sent", status_code=303)
            except Exception:
                logger.exception("TR relink error")
                return RedirectResponse("/posiciones?tr_error=Error+al+vincular+dispositivo", status_code=303)
        logger.exception("TR sync error")
        return RedirectResponse("/posiciones?tr_error=Error+en+Trade+Republic", status_code=303)

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
        # ISINs US/CA no deben tener sufijo de bolsa europea
        if country in ("US", "CA"):
            if "." in ticker:
                stale_isins.append(isin)
            continue
        preferred_exch = _ISIN_COUNTRY_TO_EXCH.get(country)
        if not preferred_exch:
            continue
        expected_suffix = _OPENFIGI_SUFFIX[preferred_exch]
        if not ticker.endswith(expected_suffix):
            stale_isins.append(isin)
    for isin in stale_isins:
        bad_ticker = isin_map_yaml.pop(isin)
        portfolio_yaml.pop(bad_ticker, None)
        delete_position(bad_ticker)
        yaml_changed = True
        logger.info("TR: ticker %s no coincide con país del ISIN %s, se re-resolverá", bad_ticker, isin)

    # Recoger tickers nuevos para enriquecer con yfinance al final
    new_tickers = []

    for pos in positions:
        if pos.get("matched") and pos.get("ticker"):
            ticker = pos["ticker"]
            upsert_position(ticker, pos["shares"], pos["avg_price"])
            synced += 1
            if ticker not in portfolio_yaml:
                portfolio_yaml[ticker] = {"name": pos.get("name", ticker)}
                new_tickers.append(ticker)
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
                if ticker not in portfolio_yaml:
                    portfolio_yaml[ticker] = {"name": pos.get("name", ticker)}
                    new_tickers.append(ticker)
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

    # Enriquecer tickers sin block/region con datos de yfinance
    tickers_to_enrich = [
        t for t, m in portfolio_yaml.items()
        if isinstance(m, dict) and (not m.get("block") or not m.get("region"))
    ]
    if tickers_to_enrich:
        def _enrich_all():
            for t in tickers_to_enrich:
                portfolio_yaml[t] = _enrich_ticker_meta(t, portfolio_yaml.get(t, {}))
        await asyncio.get_running_loop().run_in_executor(_executor, _enrich_all)
        yaml_changed = True

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

# ── Exportaciones ─────────────────────────────────────────────────────────────

@app.get("/export/portfolio")
async def export_portfolio(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    df = _read_csv()
    positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}
    rows = []
    if df is not None:
        for _, row in df[df["category"] == "portfolio"].iterrows():
            d = row.to_dict()
            ticker = d["ticker"]
            shares = avg = pnl = value = None
            if ticker in positions:
                shares, avg = positions[ticker]
                p = d.get("price")
                if p and not _is_nan(p):
                    value = shares * p
                    if avg:
                        pnl = (p - avg) / avg * 100
            rows.append({
                "ticker": ticker, "name": d.get("name", ""),
                "price": d.get("price", ""), "drawdown_52w": d.get("drawdown_52w", ""),
                "momentum_3m": d.get("momentum_3m", ""), "score": d.get("score", ""),
                "shares": shares or "", "avg_price": avg or "",
                "value_eur": round(value, 2) if value else "",
                "pnl_pct": round(pnl, 2) if pnl else "",
            })

    def _gen():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else
                           ["ticker", "name", "price", "drawdown_52w", "momentum_3m",
                            "score", "shares", "avg_price", "value_eur", "pnl_pct"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
        yield buf.getvalue()

    return StreamingResponse(
        _gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=portfolio.csv"},
    )


@app.get("/export/watchlist")
async def export_watchlist(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    df = _read_csv()
    rows = []
    if df is not None:
        for _, row in df[df["category"] == "watchlist"].sort_values("score", ascending=False).iterrows():
            d = row.to_dict()
            rows.append({
                "ticker": d["ticker"], "name": d.get("name", ""),
                "price": d.get("price", ""), "score": d.get("score", ""),
                "opportunity": d.get("opportunity", ""),
                "drawdown_52w": d.get("drawdown_52w", ""),
                "momentum_3m": d.get("momentum_3m", ""),
                "dividend_yield": d.get("dividend_yield", ""),
                "pe_ratio": d.get("pe_ratio", ""),
            })

    def _gen():
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else
                           ["ticker", "name", "price", "score", "opportunity",
                            "drawdown_52w", "momentum_3m", "dividend_yield", "pe_ratio"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
        yield buf.getvalue()

    return StreamingResponse(
        _gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=watchlist.csv"},
    )


# ── Importación masiva de tickers ─────────────────────────────────────────────

_TICKER_RE = re.compile(r'^[A-Z0-9.\-]{1,12}$')

@app.post("/tickers/import")
async def tickers_import(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    file:       UploadFile = File(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)

    content = (await file.read()).decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))
    tickers_data = _load_tickers()
    imported = 0
    errors = []
    for i, row in enumerate(reader, start=2):
        ticker = (row.get("ticker") or "").strip().upper()
        categoria = (row.get("categoria") or "watchlist").strip().lower()
        nombre = (row.get("nombre") or row.get("name") or ticker).strip()
        bloque = (row.get("bloque") or row.get("block") or "").strip()
        region = (row.get("region") or "").strip()
        if not _TICKER_RE.match(ticker):
            errors.append(f"Fila {i}: ticker inválido '{ticker}'")
            continue
        if categoria not in ("portfolio", "watchlist"):
            categoria = "watchlist"
        tickers_data.setdefault(categoria, {})[ticker] = {
            "name": nombre, "block": bloque or None, "region": region or None,
        }
        imported += 1

    if imported:
        _save_tickers(tickers_data)
    msg = f"import_ok={imported}&import_errors={len(errors)}"
    return RedirectResponse(f"/tickers?{msg}", status_code=303)


_REPORTS_PER_PAGE = 10

@app.get("/reportes", response_class=HTMLResponse)
async def reportes_page(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    page: int = 1,
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    page = max(1, page)
    offset = (page - 1) * _REPORTS_PER_PAGE
    total = count_reports()
    total_pages = max(1, (total + _REPORTS_PER_PAGE - 1) // _REPORTS_PER_PAGE)
    return templates.TemplateResponse("reportes.html", {
        "request":      request,
        "reports":      get_recent_reports(n=_REPORTS_PER_PAGE, offset=offset),
        "page":         page,
        "total_pages":  total_pages,
        "total":        total,
    })


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8589")))
