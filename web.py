"""
Interfaz web para Market Radar AI.
Uso: uvicorn web:app --host 0.0.0.0 --port 8589
     python web.py
"""
import asyncio
import csv
import datetime
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
import numpy as np
import pandas as pd
import yaml
import yfinance as yf

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from ai_analysis import (
    analyze,
    explain_ticker,
    suggest_rebalance,
    detect_news_patterns,
    analyze_operations,
    suggest_ticker_meta,
)
from config import OUTPUT_DIR
from database import (
    add_operation,
    add_price_alert,
    count_operations,
    count_reports,
    deactivate_alert,
    delete_operation,
    delete_position,
    get_active_alerts,
    get_alert_history,
    get_all_positions,
    get_operations,
    get_portfolio_value_history,
    get_recent_reports,
    get_ticker_history,
    get_tr_cache,
    init_db,
    log_alert_triggered,
    save_portfolio_value,
    save_report,
    save_snapshot,
    set_tr_cache,
    upsert_position,
    get_setting,
    set_setting,
    delete_setting,
    get_all_settings,
    upsert_push_subscription,
    delete_push_subscription,
)
from fetch_data import get_macro_context, get_news, to_eur
from generate_csv import generate
from scoring import score_watchlist, score_by_horizon, suggest_horizon, HORIZON_META

logger = logging.getLogger("web")

# ── Config ────────────────────────────────────────────────────────────────────

CREDENTIALS_FILE      = "data/credentials.json"
INITIAL_PASSWORD_FILE = "data/initial-password.txt"
TOTP_SECRET_FILE      = "data/totp_secret.key"
DEFAULT_USERNAME      = "admin"

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
_csrf_lock = threading.Lock()

# Tokens temporales en memoria: token → timestamp de expiración
_pending_tokens: dict = {}

# Lockout de login: ip → lista de timestamps de fallos recientes
_LOCKOUT_MAX      = 5
_LOCKOUT_DURATION = 900  # 15 minutos (en segundos)
_failed_logins: dict = {}

# Limpieza periódica de sesiones y tokens expirados
_last_cleanup: float = 0.0
_cleanup_lock = threading.Lock()

# Caché del CSV en memoria
_csv_cache: dict = {"df": None, "ts": 0.0}
_CSV_CACHE_TTL   = 300.0  # 5 minutos
_csv_cache_lock  = threading.RLock()


# ── Credential helpers ────────────────────────────────────────────────────────

_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_\-\.]{3,32}$')
_TICKER_RE   = re.compile(r'^[A-Z0-9.\-]{1,12}$')


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12)).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        # Timing-safe: consumir tiempo similar al de un checkpw real (mismo coste que _hash_password)
        bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(12))
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
        initial_content = (
            "=" * 52 + "\n"
            "  PRIMER ARRANQUE — Credenciales iniciales\n"
            f"  Usuario:     {DEFAULT_USERNAME}\n"
            f"  Contraseña:  {initial_password}\n"
            "  Cambia estas credenciales en el asistente de\n"
            "  primer acceso antes de usar la aplicación.\n"
            "  Elimina este archivo después de cambiar las credenciales.\n"
            + "=" * 52 + "\n"
        )
        with open(INITIAL_PASSWORD_FILE, "w") as f:
            f.write(initial_content)
        os.chmod(INITIAL_PASSWORD_FILE, 0o600)
        logger.warning("Ver data/initial-password.txt para las credenciales iniciales")
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
    # Eliminar el archivo de contraseña inicial si existe
    try:
        os.remove(INITIAL_PASSWORD_FILE)
    except FileNotFoundError:
        pass


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
init_db()  # Asegura migraciones de BD tanto en uvicorn directo como vía __main__
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
    global _last_cleanup
    now = _time.monotonic()
    if now - _last_cleanup > 60:
        with _cleanup_lock:
            if now - _last_cleanup > 60:
                _last_cleanup = now
                _cleanup_expired_state()
    if not session:
        return False
    with _cleanup_lock:
        if session not in _active_sessions:
            return False
        if now > _active_sessions[session]:
            del _active_sessions[session]
            return False
    return True

def _invalidate_session(session: Optional[str]) -> None:
    if session:
        _active_sessions.pop(session, None)

def _cleanup_expired_state() -> None:
    """Elimina sesiones y tokens pendientes expirados para evitar crecimiento ilimitado."""
    now = _time.monotonic()
    expired_sessions = [sid for sid, exp in list(_active_sessions.items()) if now > exp]
    for sid in expired_sessions:
        _active_sessions.pop(sid, None)
    expired_tokens = [tok for tok, exp in list(_pending_tokens.items()) if now > exp]
    for tok in expired_tokens:
        _pending_tokens.pop(tok, None)

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
        with _csrf_lock:
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

_KNOWN_TICKERS_KEYS = {"portfolio", "watchlist", "tr_isin_map"}


def _validate_tickers_schema(data: dict) -> dict:
    """Valida y sanea la estructura básica de tickers.yaml. Devuelve solo las partes válidas."""
    if not isinstance(data, dict):
        logger.error("tickers.yaml: estructura inválida (se esperaba un dict)")
        return {}
    result = {}
    for key, value in data.items():
        if key not in _KNOWN_TICKERS_KEYS:
            logger.warning("tickers.yaml: clave desconocida '%s', ignorada", key)
            continue
        if key in ("portfolio", "watchlist"):
            if value is None:
                result[key] = {}
            elif isinstance(value, dict):
                result[key] = value
            else:
                logger.error("tickers.yaml: '%s' debe ser un dict, ignorado", key)
        else:
            result[key] = value
    return result


def _load_tickers() -> dict:
    try:
        with open("tickers.yaml") as f:
            data = yaml.safe_load(f) or {}
        return _validate_tickers_schema(data)
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as e:
        logger.error("tickers.yaml inválido: %s", e)
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


def _sanitize_name(s: str) -> str:
    """Elimina caracteres de control y limita la longitud para evitar inyección YAML."""
    return re.sub(r'[\x00-\x1f\x7f]', '', s.strip())[:100]


def _enrich_ticker_meta(ticker: str, meta: dict) -> dict:
    """Rellena name, block, region y horizon en meta usando yfinance si están vacíos."""
    needs_name    = not meta.get("name") or meta.get("name") == ticker
    needs_block   = not meta.get("block")
    needs_region  = not meta.get("region")
    needs_horizon = not meta.get("horizon")
    if not (needs_name or needs_block or needs_region or needs_horizon):
        return meta
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info  = stock.info or {}
        if needs_name:
            long_name = info.get("longName") or info.get("shortName") or ""
            if long_name:
                meta["name"] = _sanitize_name(long_name)
        if needs_block:
            sector = info.get("sector", "")
            meta["block"] = _SECTOR_TO_BLOCK.get(sector, sector or None)
        if needs_region:
            country = info.get("country", "")
            meta["region"] = _COUNTRY_TO_REGION.get(country, country or None)
        if needs_horizon:
            roe = info.get("returnOnEquity")
            pe  = info.get("trailingPE")
            div = info.get("dividendYield")
            vol = None
            try:
                h = stock.history(period="1y")
                if not h.empty:
                    ret = h["Close"].pct_change().dropna()
                    vol = float(ret.std() * (252 ** 0.5) * 100) if not ret.empty else None
            except Exception:
                pass
            roe_pct = round(roe * 100, 1) if roe and not math.isnan(roe) else None
            div_pct = round(div * 100, 1) if div and not math.isnan(div) else None
            pe_val  = round(pe,  1)       if pe  and not math.isnan(pe)  else None
            horizon = suggest_horizon(roe_pct, pe_val, div_pct, vol, None)
            if horizon:
                meta["horizon"] = horizon
    except Exception:
        logger.exception("Error enriqueciendo meta de %s", ticker)
    return meta


def _read_csv() -> Optional[pd.DataFrame]:
    with _csv_cache_lock:
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
    with _csv_cache_lock:
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

    # Guardar valor total de la cartera
    try:
        positions = get_all_positions()
        total_val = 0.0
        for ticker_p, shares_p, _ in positions:
            row_p = df[df["ticker"] == ticker_p]
            if not row_p.empty:
                price_p = row_p.iloc[0].get("price")
                if price_p and not (isinstance(price_p, float) and math.isnan(price_p)):
                    total_val += shares_p * float(price_p)
        if total_val > 0:
            save_portfolio_value(total_val, len(positions))
    except Exception:
        logger.exception("Error guardando valor de cartera")

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
        try:
            plt.savefig(buf, format="png", dpi=110, facecolor=_C_BG, bbox_inches="tight")
        finally:
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


@app.get("/chart/valor-cartera")
async def chart_valor_cartera(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)
    rows = get_portfolio_value_history(days=365)
    if len(rows) < 2:
        raise HTTPException(404, "Sin historial suficiente")

    def _make():
        dates = pd.to_datetime([r[0] for r in rows])
        values = [r[1] for r in rows]
        fig, ax = plt.subplots(figsize=(9, 3.5))
        _style_ax(ax, fig)
        ax.fill_between(dates, values, alpha=0.15, color=_C_BLUE)
        ax.plot(dates, values, color=_C_BLUE, linewidth=1.8)
        ax.scatter([dates[-1]], [values[-1]], color=_C_GREEN, zorder=5, s=50)
        ax.set_title("Evolución del valor de la cartera", fontsize=12, pad=8)
        ax.set_ylabel("Valor (€)", fontsize=9)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"€{v:,.0f}"))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate(rotation=30)
        fig.tight_layout()
        return fig

    with _chart_lock:
        fig = await asyncio.get_running_loop().run_in_executor(_executor, _make)
    return _fig_to_response(fig)


@app.get("/cartera/valor-historico")
async def valor_historico(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)
    rows = get_portfolio_value_history(days=90)
    return JSONResponse([{"date": r[0], "total": r[1]} for r in rows])


@app.get("/chart/benchmark")
async def chart_benchmark(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)

    rows = get_portfolio_value_history(days=365)
    if len(rows) < 5:
        raise HTTPException(404, "Sin historial suficiente (se necesitan al menos 5 días)")

    start_date = rows[0][0]

    def _make():
        try:
            spy = yf.Ticker("SPY").history(start=start_date)["Close"]
            ewq = yf.Ticker("EWQ").history(start=start_date)["Close"]
        except Exception:
            spy = pd.Series(dtype=float)
            ewq = pd.Series(dtype=float)

        dates_pf = pd.to_datetime([r[0] for r in rows])
        values_pf = [r[1] for r in rows]

        # Normalize to 100 at start
        base_pf = values_pf[0]
        norm_pf = [v / base_pf * 100 for v in values_pf]

        fig, ax = plt.subplots(figsize=(9, 4))
        _style_ax(ax, fig)

        ax.plot(dates_pf, norm_pf, color=_C_BLUE, linewidth=2, label="Mi cartera", zorder=3)

        if not spy.empty:
            spy_norm = spy / spy.iloc[0] * 100
            ax.plot(spy.index, spy_norm.values, color=_C_GREEN, linewidth=1.2, linestyle="--", label="SPY (S&P500)", alpha=0.8)

        if not ewq.empty:
            ewq_norm = ewq / ewq.iloc[0] * 100
            ax.plot(ewq.index, ewq_norm.values, color="#d29922", linewidth=1.2, linestyle="--", label="EWQ (Euro Stoxx)", alpha=0.8)

        ax.axhline(100, color=_C_TEXT, linewidth=0.6, linestyle=":")
        ax.set_title(f"Comparativa vs benchmark (base 100 desde {start_date})", fontsize=11, pad=8)
        ax.set_ylabel("Rendimiento (base 100)", fontsize=9)
        ax.legend(fontsize=8, facecolor=_C_CARD, edgecolor=_C_GRID, labelcolor=_C_FG)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate(rotation=30)
        fig.tight_layout()
        return fig

    with _chart_lock:
        fig = await asyncio.get_running_loop().run_in_executor(_executor, _make)
    return _fig_to_response(fig)


# ── QR code ───────────────────────────────────────────────────────────────────

def _make_qr_svg(uri: str) -> str:
    buf = io.BytesIO()
    try:
        segno.make_qr(uri).save(buf, kind="svg", scale=4, border=1, xmldecl=False, nl=False)
        return buf.getvalue().decode("utf-8")
    finally:
        buf.close()



# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
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
        "timestamp":  datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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

    resp = RedirectResponse("/settings/app?setup=1", status_code=303)
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


# ── Configuración de la aplicación ────────────────────────────────────────────

# Variables configurables desde la web (BD sobrescribe variable de entorno)
_APP_SETTINGS = [
    {
        "key": "ANTHROPIC_API_KEY",
        "label": "Anthropic API Key",
        "hint": "Clave de la API de Claude. Formato: sk-ant-...",
        "type": "password",
        "restart": False,
        "section": "Claude AI",
    },
    {
        "key": "MODEL",
        "label": "Modelo Claude",
        "hint": "Modelo que se usa para generar informes y traducir noticias.",
        "type": "select",
        "options": [
            "claude-haiku-4-5-20251001",
            "claude-haiku-4-5",
            "claude-sonnet-4-5",
            "claude-sonnet-4-6",
            "claude-opus-4-5",
            "claude-opus-4-6",
        ],
        "restart": False,
        "section": "Claude AI",
    },
    {
        "key": "TELEGRAM_BOT_TOKEN",
        "label": "Telegram Bot Token",
        "hint": "Token del bot obtenido de @BotFather.",
        "type": "password",
        "restart": True,
        "section": "Telegram",
    },
    {
        "key": "TELEGRAM_CHAT_ID",
        "label": "Telegram Chat ID",
        "hint": "Tu ID de Telegram (destinatario de alertas y reportes).",
        "type": "text",
        "restart": False,
        "section": "Telegram",
    },
    {
        "key": "REPORT_HOUR",
        "label": "Hora del informe diario (0–23)",
        "hint": "Hora local (según TIMEZONE) en que se envía el reporte diario.",
        "type": "number",
        "restart": True,
        "section": "Planificación",
    },
    {
        "key": "TIMEZONE",
        "label": "Zona horaria",
        "hint": "Zona horaria IANA, p.ej. Europe/Madrid.",
        "type": "text",
        "restart": True,
        "section": "Planificación",
    },
    {
        "key": "TR_PHONE",
        "label": "Trade Republic — Teléfono",
        "hint": "Número de teléfono asociado a tu cuenta Trade Republic (con prefijo, ej. +34...).",
        "type": "text",
        "restart": False,
        "section": "Trade Republic",
    },
    {
        "key": "TR_PIN",
        "label": "Trade Republic — PIN",
        "hint": "PIN de 4 dígitos de tu cuenta Trade Republic.",
        "type": "password",
        "restart": False,
        "section": "Trade Republic",
    },
]


def _missing_required_settings() -> list[str]:
    """Devuelve lista de claves requeridas que no están configuradas (ni en BD ni en env)."""
    from config import ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    db = get_all_settings()
    missing = []
    for key, env_val in [
        ("ANTHROPIC_API_KEY",  ANTHROPIC_API_KEY),
        ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID",   TELEGRAM_CHAT_ID),
    ]:
        if not db.get(key) and not env_val:
            missing.append(key)
    return missing


@app.get("/settings/app", response_class=HTMLResponse)
async def settings_app_page(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    ok: str = "",
    error: str = "",
    setup: str = "",
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    db_settings = get_all_settings()
    env_settings = {s["key"]: os.environ.get(s["key"], "") for s in _APP_SETTINGS}
    return templates.TemplateResponse("settings_app.html", {
        "request":      request,
        "settings":     _APP_SETTINGS,
        "db_settings":  db_settings,
        "env_settings": env_settings,
        "ok":           ok,
        "error":        error,
        "setup":        setup,
        "missing":      _missing_required_settings(),
    })


@app.post("/settings/app")
async def settings_app_update(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    form = await request.form()
    valid_keys = {s["key"] for s in _APP_SETTINGS}
    for key in valid_keys:
        value = (form.get(key) or "").strip()
        clear = form.get(f"clear_{key}")
        if clear:
            delete_setting(key)
        elif value:
            set_setting(key, value)
        # Empty + no clear → leave unchanged
    return RedirectResponse("/settings/app?ok=1", status_code=303)


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
            pnl_eur = (price - avg) * shares if (
                avg and price and not _is_nan(price) and shares
            ) else None
            d["shares"]    = shares
            d["avg_price"] = avg
            d["value"]     = value
            d["pnl_pct"]   = pnl
            d["pnl_eur"]   = pnl_eur
            portfolio.append(d)

        for _, row in df_s[df_s["category"] == "watchlist"] \
                          .sort_values("score", ascending=False).iterrows():
            watchlist.append(row.to_dict())

    reports = get_recent_reports(n=1)

    tr_cash_row = get_tr_cache("cash_eur")
    tr_cash = None
    if tr_cash_row:
        try:
            tr_cash = float(tr_cash_row[0])
        except (TypeError, ValueError):
            tr_cash = None
    if tr_cash and total_value is not None:
        total_value += tr_cash

    # Antigüedad de datos: fecha de modificación del CSV
    csv_path = f"{OUTPUT_DIR}/precios_global.csv"
    data_age = None
    if os.path.exists(csv_path):
        mtime = os.path.getmtime(csv_path)
        age_sec = _time.time() - mtime
        if age_sec < 3600:
            data_age = f"hace {int(age_sec // 60)} min"
        elif age_sec < 86400:
            data_age = f"hace {int(age_sec // 3600)} h"
        else:
            data_age = datetime.datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")

    has_value_history = len(get_portfolio_value_history(days=365)) >= 2

    return templates.TemplateResponse("dashboard.html", {
        "request":           request,
        "portfolio":         portfolio,
        "watchlist":         watchlist,
        "total_value":       total_value if total_value else None,
        "last_report":       reports[0] if reports else None,
        "has_data":          df is not None,
        "n_alerts":          len(get_active_alerts()),
        "n_tickers":         len(portfolio) + len(watchlist),
        "tr_cash":           tr_cash,
        "data_age":          data_age,
        "error":             error,
        "has_value_history": has_value_history,
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
                _tw_raw = row.get("target_weight")
                tw = float(_tw_raw) if _tw_raw is not None and not _is_nan(_tw_raw) else None
            except (TypeError, ValueError):
                tw = None
            horizon_val = row.get("horizon")
            rows_data.append({
                "ticker":   ticker,
                "name":     row["name"],
                "shares":   shares,
                "price":    float(price),
                "value":    value,
                "target_w": tw,
                "horizon":  horizon_val if horizon_val and str(horizon_val) != "nan" else None,
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
        "horizon_meta":  HORIZON_META,
    })


# ── Oportunidades ─────────────────────────────────────────────────────────────

@app.get("/oportunidades", response_class=HTMLResponse)
async def oportunidades_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df = _read_csv()
    by_horizon: dict = {h: [] for h in ("corto", "medio", "largo")}

    if df is not None:
        df_s = score_by_horizon(df)
        for _, row in df_s.iterrows():
            h = row.get("horizon") or "medio"
            if h not in by_horizon:
                h = "medio"
            if row.get("opportunity") in ("ALTA", "MEDIA"):
                by_horizon[h].append(row.to_dict())

        # Ordenar cada horizonte por score descendente
        for h in by_horizon:
            by_horizon[h].sort(key=lambda r: -(r.get("score") or 0))

    total_opps = sum(len(v) for v in by_horizon.values())

    return templates.TemplateResponse("oportunidades.html", {
        "request":      request,
        "by_horizon":   by_horizon,
        "horizon_meta": HORIZON_META,
        "total_opps":   total_opps,
        "has_data":     df is not None,
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

    fetch_start = datetime.datetime.now()
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
    if not _is_auth(session):
        return JSONResponse([])
    if len(q) < 2 or len(q) > 50:
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
    if not _is_auth(session) or not ticker:
        return JSONResponse({})
    def _do_info():
        import yfinance as yf
        import math as _math
        try:
            stock = yf.Ticker(ticker)
            info  = stock.info or {}
            sector  = info.get("sector", "")
            country = info.get("country", "")
            roe     = info.get("returnOnEquity")
            pe      = info.get("trailingPE")
            div     = info.get("dividendYield")
            # volatility: std anualizada de últimos 252 días
            vol = None
            try:
                h = stock.history(period="1y")
                if not h.empty:
                    ret = h["Close"].pct_change().dropna()
                    vol = float(ret.std() * (252 ** 0.5) * 100) if not ret.empty else None
            except Exception:
                pass
            roe_pct  = round(roe * 100, 1)  if roe  and not _math.isnan(roe)  else None
            div_pct  = round(div * 100, 1)  if div  and not _math.isnan(div)  else None
            pe_val   = round(pe, 1)         if pe   and not _math.isnan(pe)   else None
            mom3m    = None  # no disponible en este endpoint rápido
            horizon  = suggest_horizon(roe_pct, pe_val, div_pct, vol, mom3m)
            if horizon not in HORIZON_META:
                horizon = "medio"
            return {
                "name":            info.get("longName") or info.get("shortName", ticker),
                "block":           _SECTOR_TO_BLOCK.get(sector, sector),
                "region":          _COUNTRY_TO_REGION.get(country, country),
                "horizon":         horizon,
                "horizon_label":   HORIZON_META[horizon]["label"],
                "horizon_range":   HORIZON_META[horizon]["range"],
                "horizon_desc":    HORIZON_META[horizon]["desc"],
            }
        except Exception:
            return {}
    result = await asyncio.get_running_loop().run_in_executor(_executor, _do_info)
    return JSONResponse(result)


@app.get("/tickers", response_class=HTMLResponse)
async def tickers_page(
    request: Request,
    tab:     Optional[str] = None,   # 'tickers' (default) | 'tr'
    saved:   Optional[str] = None,
    error:   Optional[str] = None,
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    tickers = _load_tickers()
    df      = _read_csv()

    # ── Posiciones ──
    yaml_names: dict = {}
    for cat in ("portfolio", "watchlist"):
        for t, meta in (tickers.get(cat) or {}).items():
            if isinstance(meta, dict) and meta.get("name"):
                yaml_names[t] = meta["name"]

    pos_data = []
    for ticker_row, shares, avg_price in get_all_positions():
        price = pnl = value = name = None
        if df is not None:
            row = df[df["ticker"] == ticker_row]
            if not row.empty:
                r     = row.iloc[0]
                price = r.get("price")
                name  = r.get("name")
                if price and not _is_nan(price) and avg_price:
                    pnl   = (price - avg_price) / avg_price * 100
                    value = shares * price
        if not name or name == ticker_row:
            name = yaml_names.get(ticker_row, ticker_row)
        pnl_eur = (price - avg_price) * shares if (
            price and not _is_nan(price) and avg_price
        ) else None
        split_warning = (
            price and avg_price and not _is_nan(price)
            and price > 0 and avg_price > 0
            and (price / avg_price) < 0.15
        )
        pos_data.append({
            "ticker": ticker_row, "name": name, "shares": shares,
            "avg_price": avg_price, "price": price,
            "pnl": pnl, "value": value,
            "pnl_eur": pnl_eur, "split_warning": split_warning,
        })

    # ── Trade Republic ──
    tr_cash_row      = get_tr_cache("cash_eur")
    tr_unmatched_row = get_tr_cache("tr_unmatched")
    tr_unmatched: list = []
    if tr_unmatched_row:
        try:
            tr_unmatched = json.loads(tr_unmatched_row[0])
        except Exception:
            pass

    return templates.TemplateResponse("tickers.html", {
        "request":               request,
        "portfolio":             tickers.get("portfolio", {}),
        "watchlist":             tickers.get("watchlist", {}),
        "tickers_with_position": {p["ticker"] for p in pos_data},
        "positions":             pos_data,
        "tr_status":             _tr_status(),
        "tr_cash":               float(tr_cash_row[0]) if tr_cash_row else None,
        "tr_unmatched":          tr_unmatched,
        "active_tab":            tab if tab in ("tickers", "tr") else "tickers",
        "saved":                 saved,
        "error":                 error,
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
    horizon:       str = Form(""),
    target_price:  str = Form(""),
    notes:         str = Form(""),
    csrf_token:    Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    if categoria not in ("portfolio", "watchlist"):
        return RedirectResponse("/tickers", status_code=303)
    t = ticker.strip().upper()
    if not _TICKER_RE.match(t):
        return RedirectResponse("/tickers?error=ticker_invalido", status_code=303)
    tickers = _load_tickers()
    tickers.setdefault(categoria, {})
    entry: dict = {"name": _sanitize_name(nombre), "block": bloque, "region": region}
    if target_weight:
        try:
            entry["target_weight"] = float(target_weight)
        except ValueError:
            pass
    if horizon in ("largo", "medio", "corto"):
        entry["horizon"] = horizon
    if target_price:
        try:
            entry["target_price"] = float(target_price)
        except ValueError:
            pass
    if notes:
        entry["notes"] = notes.strip()[:500]
    tickers[categoria][t] = entry
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
    horizon:       str = Form(""),
    target_price:  str = Form(""),
    notes:         str = Form(""),
    csrf_token:    Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    if categoria not in ("portfolio", "watchlist"):
        return RedirectResponse("/tickers", status_code=303)
    tickers = _load_tickers()
    if ticker not in tickers.get(categoria, {}):
        return RedirectResponse("/tickers", status_code=303)
    meta = tickers[categoria][ticker]
    if nombre:        meta["name"]          = _sanitize_name(nombre)
    if bloque:        meta["block"]         = bloque
    if region:        meta["region"]        = region
    if target_weight:
        try:
            meta["target_weight"] = float(target_weight)
        except ValueError:
            pass
    if horizon in ("largo", "medio", "corto"):
        meta["horizon"] = horizon
    if target_price:
        try:
            meta["target_price"] = float(target_price)
        except ValueError:
            pass
    if notes is not None:
        meta["notes"] = notes.strip()[:500]
    tickers.setdefault(categoria, {})[ticker] = meta
    _save_tickers(tickers)
    return RedirectResponse("/tickers", status_code=303)


@app.post("/tickers/enrich")
async def tickers_enrich(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    """Rellena block, region y horizon de todos los tickers que les falten usando yfinance."""
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    tickers_data = _load_tickers()
    changed = False
    def _do_enrich():
        nonlocal changed
        for cat in ("portfolio", "watchlist"):
            for ticker, meta in (tickers_data.get(cat) or {}).items():
                if not isinstance(meta, dict):
                    continue
                has_name = meta.get("name") and meta.get("name") != ticker
                if has_name and meta.get("block") and meta.get("region") and meta.get("horizon"):
                    continue
                enriched = _enrich_ticker_meta(ticker, dict(meta))
                if enriched != meta:
                    tickers_data[cat][ticker] = enriched
                    changed = True
    await asyncio.get_running_loop().run_in_executor(_executor, _do_enrich)
    if changed:
        _save_tickers(tickers_data)
    return RedirectResponse("/tickers?enriched=1", status_code=303)


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
    saved:   Optional[str] = None,
    error:   Optional[str] = None,
    session: Optional[str] = Cookie(default=None),
):
    """Redirige a la pestaña de posiciones dentro de /tickers."""
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    qs = ""
    if saved:
        qs = f"?saved={saved}"
    elif error:
        qs = f"?error={error}"
    return RedirectResponse(f"/tickers{qs}", status_code=302)


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
        return RedirectResponse(f"/tickers?saved={t}", status_code=303)
    return RedirectResponse("/tickers?error=datos_invalidos", status_code=303)


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
    return RedirectResponse("/tickers", status_code=303)


# ── Operaciones ───────────────────────────────────────────────────────────────

@app.get("/operaciones", response_class=HTMLResponse)
async def operaciones_page(
    request: Request,
    ticker: Optional[str] = None,
    saved: Optional[str] = None,
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    df = _read_csv()
    ticker_filter = ticker.upper() if ticker else None
    ops = get_operations(ticker=ticker_filter, limit=200)
    tickers_yaml = _load_tickers()
    all_tickers = []
    for cat in ("portfolio", "watchlist"):
        for t, meta in (tickers_yaml.get(cat) or {}).items():
            name = meta.get("name", t) if isinstance(meta, dict) else t
            all_tickers.append({"ticker": t, "name": name})
    all_tickers.sort(key=lambda x: x["ticker"])

    # Compute current prices for P&L calculation
    prices = {}
    if df is not None:
        for _, row in df.iterrows():
            prices[row["ticker"]] = row.get("price")

    return templates.TemplateResponse("operaciones.html", {
        "request": request,
        "ops": ops,
        "all_tickers": all_tickers,
        "ticker_filter": ticker_filter,
        "saved": saved,
        "count": count_operations(),
    })


@app.post("/operaciones/add")
async def operaciones_add(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    ticker:     str   = Form(...),
    date:       str   = Form(...),
    op_type:    str   = Form(...),
    shares:     float = Form(...),
    price_eur:  float = Form(...),
    notes:      str   = Form(""),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    t = ticker.strip().upper()
    if op_type not in ("buy", "sell") or shares <= 0 or price_eur <= 0:
        return RedirectResponse("/operaciones", status_code=303)
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        return RedirectResponse("/operaciones", status_code=303)
    add_operation(t, date, op_type, shares, price_eur, notes.strip()[:500])
    return RedirectResponse(f"/operaciones?saved={t}", status_code=303)


@app.post("/operaciones/delete")
async def operaciones_delete(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    op_id:      int = Form(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    delete_operation(op_id)
    return RedirectResponse("/operaciones", status_code=303)


# ── Distribución ───────────────────────────────────────────────────────────────

@app.get("/distribucion", response_class=HTMLResponse)
async def distribucion_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df = _read_csv()
    positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}

    by_sector = {}
    by_region = {}
    total = 0.0

    if df is not None:
        for _, row in df.iterrows():
            ticker = row["ticker"]
            price = row.get("price")
            if not price or _is_nan(price):
                continue
            cat = row.get("category", "watchlist")
            if cat == "portfolio" and ticker in positions:
                shares, _ = positions[ticker]
                value = shares * float(price)
            else:
                continue  # Only show distribution for portfolio positions

            total += value
            sector = row.get("block") or "Sin sector"
            region = row.get("region") or "Sin región"

            by_sector[sector] = by_sector.get(sector, 0.0) + value
            by_region[region] = by_region.get(region, 0.0) + value

    def _to_pct_list(d, total):
        items = sorted(d.items(), key=lambda x: -x[1])
        return [{"label": k, "value": v, "pct": round(v / total * 100, 1) if total else 0} for k, v in items]

    return templates.TemplateResponse("distribucion.html", {
        "request": request,
        "by_sector": _to_pct_list(by_sector, total),
        "by_region": _to_pct_list(by_region, total),
        "total": total,
        "has_data": df is not None,
        "has_positions": bool(positions),
    })


# ── Simulador de aportación ───────────────────────────────────────────────────

@app.get("/simulador", response_class=HTMLResponse)
async def simulador_page(
    request: Request,
    importe: float = 0.0,
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df = _read_csv()
    positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}
    suggestions = []

    if df is not None and importe > 0 and positions:
        rows_data = []
        total_value = 0.0
        for _, row in df[df["category"] == "portfolio"].iterrows():
            ticker = row["ticker"]
            if ticker not in positions:
                continue
            price = row.get("price")
            if not price or _is_nan(price):
                continue
            shares, _ = positions[ticker]
            value = shares * float(price)
            total_value += value
            try:
                _tw_raw = row.get("target_weight")
                tw = float(_tw_raw) if _tw_raw is not None and not _is_nan(_tw_raw) else None
            except (TypeError, ValueError):
                tw = None
            rows_data.append({
                "ticker": ticker,
                "name": row["name"],
                "price": float(price),
                "value": value,
                "target_w": tw,
                "score": row.get("score", 0) or 0,
            })

        total_with_new = total_value + importe

        for r in rows_data:
            r["current_w"] = r["value"] / total_value * 100 if total_value else 0
            r["target_w_eff"] = r["target_w"] if r["target_w"] else (100 / len(rows_data) if rows_data else 0)

        # Compute how much each position needs to reach target weight in new total
        for r in rows_data:
            target_value = total_with_new * r["target_w_eff"] / 100
            deficit = target_value - r["value"]
            r["deficit"] = max(0, deficit)

        total_deficit = sum(r["deficit"] for r in rows_data)

        for r in rows_data:
            if total_deficit > 0:
                alloc = importe * r["deficit"] / total_deficit
            else:
                # Equal distribution if no deficits
                alloc = importe / len(rows_data) if rows_data else 0
            r["alloc"] = round(alloc, 2)
            r["shares_to_buy"] = alloc / r["price"] if r["price"] > 0 and alloc > 0 else 0

        suggestions = [r for r in rows_data if r["alloc"] > 0.01]
        suggestions.sort(key=lambda x: -x["alloc"])

    return templates.TemplateResponse("simulador.html", {
        "request": request,
        "importe": importe,
        "suggestions": suggestions,
        "has_data": df is not None,
        "has_positions": bool(positions),
    })


# ── Benchmark ─────────────────────────────────────────────────────────────────

@app.get("/benchmark", response_class=HTMLResponse)
async def benchmark_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    positions = get_all_positions()
    has_positions = bool(positions)
    value_history = get_portfolio_value_history(days=365)
    has_history = len(value_history) >= 5

    return templates.TemplateResponse("benchmark.html", {
        "request": request,
        "has_positions": has_positions,
        "has_history": has_history,
        "value_history": value_history,
    })


# ── Screener ───────────────────────────────────────────────────────────────────

@app.get("/screener", response_class=HTMLResponse)
async def screener_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df = _read_csv()
    rows = []
    sectors = set()
    regions = set()

    if df is not None:
        df_s = score_watchlist(df)
        for _, row in df_s.iterrows():
            d = row.to_dict()
            sector = d.get("block")
            region = d.get("region")
            sector = sector if isinstance(sector, str) and sector else ""
            region = region if isinstance(region, str) and region else ""
            if sector:
                sectors.add(sector)
            if region:
                regions.add(region)
            rows.append(d)
        rows.sort(key=lambda x: -(x.get("score") or 0))

    return templates.TemplateResponse("screener.html", {
        "request": request,
        "rows": rows,
        "sectors": sorted(sectors),
        "regions": sorted(regions),
        "has_data": df is not None,
    })


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
    t = ticker.strip().upper()

    if condition_type == "stoploss_pct":
        # Stop-loss dinámico: % de pérdida desde precio de compra
        pct = abs(target_price)
        if not (0 < pct <= 100):
            return RedirectResponse("/alertas?error=rango", status_code=303)
        add_price_alert(t, pct, "below", condition_type="stoploss_pct",
                        condition_value=pct)
        return RedirectResponse("/alertas", status_code=303)
    elif condition_type == "drawdown":
        # Los drawdowns son porcentajes negativos (-100 a 0)
        # Normalizar: si el usuario introduce positivo, convertir a negativo
        target_price = -abs(target_price)
        if not (-100 <= target_price < 0):
            return RedirectResponse("/alertas?error=rango", status_code=303)
        add_price_alert(t, target_price, "below", condition_type=condition_type,
                        condition_value=target_price)
        return RedirectResponse("/alertas", status_code=303)
    elif condition_type == "score":
        # El score debe estar entre 0 y 100
        if not (0 <= target_price <= 100):
            return RedirectResponse("/alertas?error=rango", status_code=303)
        add_price_alert(t, target_price, "above", condition_type=condition_type,
                        condition_value=target_price)
        return RedirectResponse("/alertas", status_code=303)

    if not (0 < target_price < 1_000_000):
        return RedirectResponse("/alertas", status_code=303)

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
        return RedirectResponse("/tickers?tab=tr&tr_msg=sms_sent", status_code=303)
    except Exception:
        logger.exception("TR setup/start error")
        return RedirectResponse("/tickers?tab=tr&tr_error=Error+en+Trade+Republic", status_code=303)


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
        return RedirectResponse("/tickers?tab=tr&tr_msg=setup_ok", status_code=303)
    except Exception:
        logger.exception("TR setup/complete error")
        return RedirectResponse("/tickers?tab=tr&tr_error=Error+en+Trade+Republic", status_code=303)


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
                return RedirectResponse("/tickers?tab=tr&tr_msg=sms_sent", status_code=303)
            except Exception:
                logger.exception("TR relink error")
                return RedirectResponse("/tickers?tab=tr&tr_error=Error+al+vincular+dispositivo", status_code=303)
        logger.exception("TR sync error")
        return RedirectResponse("/tickers?tab=tr&tr_error=Error+en+Trade+Republic", status_code=303)

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
        if isinstance(m, dict) and (
            not m.get("block") or not m.get("region") or not m.get("horizon")
            or not m.get("name") or m.get("name") == t
        )
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
        f"/tickers?tab=tr&tr_synced={synced}&tr_unmatched={len(unmatched)}",
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
    times, values = [], []
    for item in items:
        try:
            times.append(datetime.datetime.fromtimestamp(item["time_ms"] / 1000, tz=datetime.timezone.utc))
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

# ── Dividendos ────────────────────────────────────────────────────────────────

@app.get("/dividendos", response_class=HTMLResponse)
async def dividendos_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}

    def _fetch():
        from collections import defaultdict
        results = []
        for ticker, (shares, _avg) in positions.items():
            try:
                stock  = yf.Ticker(ticker)
                info   = {}
                try:
                    info = stock.info or {}
                except Exception:
                    pass
                currency = info.get("currency", "USD")

                divs = stock.dividends
                if divs is None or divs.empty:
                    continue

                # Normalizar zona horaria
                if divs.index.tz is None:
                    divs.index = divs.index.tz_localize("UTC")
                else:
                    divs.index = divs.index.tz_convert("UTC")

                # Últimos 2 años para detectar el patrón; al menos último año para cuantías
                cutoff_2y  = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=2)
                cutoff_1y  = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=1)
                recent_2y  = divs[divs.index >= cutoff_2y]
                recent_1y  = divs[divs.index >= cutoff_1y]

                if recent_2y.empty:
                    continue

                # Base de estimación: último año si hay datos, si no los 2 años
                base = recent_1y if not recent_1y.empty else recent_2y
                n_months_base = max(1, (pd.Timestamp.now(tz="UTC") - base.index[0]).days / 30.44)

                # Promedio por mes calendario (qué meses paga y cuánto)
                monthly_avg: dict = defaultdict(list)
                for dt, amount in base.items():
                    monthly_avg[dt.month].append(float(amount))
                monthly_est = {m: sum(v) / len(v) for m, v in monthly_avg.items()}

                # Distribución trimestral
                quarterly_raw = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
                for month, avg_div in monthly_est.items():
                    quarterly_raw[(month - 1) // 3 + 1] += avg_div

                # Convertir a EUR multiplicando por las acciones
                quarterly_eur = {}
                for q, raw in quarterly_raw.items():
                    converted = to_eur(raw * shares, currency)
                    quarterly_eur[q] = round(converted, 2) if converted and not math.isnan(converted) else 0.0

                annual_eur = sum(quarterly_eur.values())
                if annual_eur <= 0:
                    continue

                # Precio actual para calcular yield sobre coste
                price_eur  = None
                try:
                    hist = stock.history(period="2d")
                    if not hist.empty:
                        p = to_eur(float(hist["Close"].iloc[-1]), currency)
                        if p and not math.isnan(p):
                            price_eur = p
                except Exception:
                    pass
                current_value = shares * price_eur if price_eur else None
                yield_pct = round(annual_eur / current_value * 100, 2) if current_value and current_value > 0 else None

                # Próxima fecha ex-dividendo
                next_exdate = None
                ex_ts = info.get("exDividendDate")
                if ex_ts:
                    try:
                        next_exdate = datetime.date.fromtimestamp(ex_ts).isoformat()
                    except (OSError, OverflowError, ValueError):
                        pass

                # Frecuencia estimada
                n_pay = len(recent_1y) if not recent_1y.empty else len(recent_2y) // 2
                if n_pay >= 10:
                    frequency = "mensual"
                elif n_pay >= 3:
                    frequency = "trimestral"
                elif n_pay >= 2:
                    frequency = "semestral"
                else:
                    frequency = "anual"

                results.append({
                    "ticker":      ticker,
                    "name":        info.get("longName") or info.get("shortName") or ticker,
                    "shares":      shares,
                    "q1_eur":      quarterly_eur[1],
                    "q2_eur":      quarterly_eur[2],
                    "q3_eur":      quarterly_eur[3],
                    "q4_eur":      quarterly_eur[4],
                    "annual_eur":  round(annual_eur, 2),
                    "yield_pct":   yield_pct,
                    "frequency":   frequency,
                    "next_exdate": next_exdate,
                })
            except Exception:
                logger.exception("Error calculando dividendos de %s", ticker)

        return sorted(results, key=lambda x: -x["annual_eur"])

    rows = await asyncio.get_running_loop().run_in_executor(_executor, _fetch)

    totals = {
        "q1_eur":     round(sum(r["q1_eur"]    for r in rows), 2),
        "q2_eur":     round(sum(r["q2_eur"]    for r in rows), 2),
        "q3_eur":     round(sum(r["q3_eur"]    for r in rows), 2),
        "q4_eur":     round(sum(r["q4_eur"]    for r in rows), 2),
        "annual_eur": round(sum(r["annual_eur"] for r in rows), 2),
    }

    return templates.TemplateResponse("dividendos.html", {
        "request": request,
        "rows":    rows,
        "totals":  totals,
        "year":    datetime.date.today().year,
    })


# ── Consenso de analistas ─────────────────────────────────────────────────────

@app.get("/analistas", response_class=HTMLResponse)
async def analistas_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    tickers_data = _load_tickers()
    all_tickers: list[tuple[str, str, str]] = []
    for cat in ("portfolio", "watchlist"):
        for t, meta in (tickers_data.get(cat) or {}).items():
            name = meta.get("name", t) if isinstance(meta, dict) else t
            all_tickers.append((t, name, cat))

    def _fetch_one(t_name_cat):
        t, name, cat = t_name_cat
        try:
            info = yf.Ticker(t).info or {}
        except Exception:
            info = {}
        rec_mean  = info.get("recommendationMean")
        rec_key   = info.get("recommendationKey", "")
        n_analysts= info.get("numberOfAnalystOpinions") or 0
        tgt_mean  = info.get("targetMeanPrice")
        tgt_high  = info.get("targetHighPrice")
        tgt_low   = info.get("targetLowPrice")
        current   = info.get("currentPrice") or info.get("regularMarketPrice")
        currency  = info.get("currency", "USD")
        # Convert targets to EUR
        if tgt_mean and currency != "EUR":
            tgt_mean = to_eur(tgt_mean, currency)
        if tgt_high and currency != "EUR":
            tgt_high = to_eur(tgt_high, currency)
        if tgt_low and currency != "EUR":
            tgt_low  = to_eur(tgt_low, currency)
        if current and currency != "EUR":
            current  = to_eur(current, currency)
        upside = None
        if tgt_mean and current and current > 0:
            upside = (tgt_mean - current) / current * 100
        return {
            "ticker":    t, "name": name, "cat": cat,
            "rec_mean":  round(rec_mean, 1) if rec_mean else None,
            "rec_key":   rec_key,
            "n":         int(n_analysts),
            "tgt_mean":  round(tgt_mean, 2) if tgt_mean else None,
            "tgt_high":  round(tgt_high, 2) if tgt_high else None,
            "tgt_low":   round(tgt_low, 2) if tgt_low else None,
            "current":   round(current, 2) if current else None,
            "upside":    round(upside, 1) if upside else None,
        }

    loop = asyncio.get_running_loop()
    from concurrent.futures import as_completed, ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fetch_one, item): item for item in all_tickers}
        rows = []
        for fut in as_completed(futs, timeout=90):
            try:
                r = fut.result()
                if r["n"] > 0 or r["rec_key"]:
                    rows.append(r)
            except Exception:
                pass
    rows.sort(key=lambda x: (-(x["upside"] or -999)))

    return templates.TemplateResponse("analistas.html", {
        "request": request, "rows": rows,
    })


# ── Earnings próximos ─────────────────────────────────────────────────────────

@app.get("/earnings", response_class=HTMLResponse)
async def earnings_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    tickers_data = _load_tickers()
    all_tickers = []
    for cat in ("portfolio", "watchlist"):
        for t, meta in (tickers_data.get(cat) or {}).items():
            name = meta.get("name", t) if isinstance(meta, dict) else t
            all_tickers.append((t, name, cat))

    def _fetch_one(t_name_cat):
        t, name, cat = t_name_cat
        try:
            stock = yf.Ticker(t)
            info  = stock.info or {}
            cal   = {}
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
            elif info.get("earningsDate"):
                ts = info["earningsDate"]
                import datetime as _dt
                if isinstance(ts, (int, float)):
                    earnings_date = _dt.date.fromtimestamp(ts)
            eps_est   = cal.get("EPS Estimate")
            rev_est   = cal.get("Revenue Estimate") or cal.get("Revenue Average")
            eps_avg   = float(eps_est) if eps_est is not None else None
            rev_avg   = float(rev_est) / 1e9 if rev_est is not None else None
            days_until = None
            if earnings_date:
                import datetime as _dt2
                today = _dt2.date.today()
                if hasattr(earnings_date, "date"):
                    earnings_date = earnings_date.date()
                days_until = (earnings_date - today).days
            return {
                "ticker": t, "name": name, "cat": cat,
                "earnings_date": str(earnings_date) if earnings_date else None,
                "days_until": days_until,
                "eps_est":  round(eps_avg, 2) if eps_avg else None,
                "rev_est_b": round(rev_avg, 2) if rev_avg else None,
            }
        except Exception:
            return {"ticker": t, "name": name, "cat": cat,
                    "earnings_date": None, "days_until": None,
                    "eps_est": None, "rev_est_b": None}

    from concurrent.futures import as_completed, ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_fetch_one, item): item for item in all_tickers}
        rows = []
        for fut in as_completed(futs, timeout=90):
            try:
                r = fut.result()
                if r["earnings_date"]:
                    rows.append(r)
            except Exception:
                pass

    upcoming = sorted([r for r in rows if r["days_until"] is not None and r["days_until"] >= 0],
                      key=lambda x: x["days_until"])
    past     = sorted([r for r in rows if r["days_until"] is not None and r["days_until"] < 0],
                      key=lambda x: -x["days_until"])

    return templates.TemplateResponse("earnings.html", {
        "request": request, "upcoming": upcoming, "past": past,
    })


# ── Correlación entre activos ──────────────────────────────────────────────────

@app.get("/correlacion", response_class=HTMLResponse)
async def correlacion_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    positions = get_all_positions()
    tickers   = [r[0] for r in positions]
    return templates.TemplateResponse("correlacion.html", {
        "request": request, "tickers": tickers, "n": len(tickers),
    })


@app.get("/chart/correlacion")
async def chart_correlacion(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(status_code=401)

    positions = get_all_positions()
    tickers   = [r[0] for r in positions]
    if len(tickers) < 2:
        raise HTTPException(status_code=400, detail="Se necesitan al menos 2 posiciones")

    def _compute_and_render():
        frames = {}
        for t in tickers:
            try:
                hist = yf.Ticker(t).history(period="1y")["Close"]
                if not hist.empty:
                    frames[t] = hist
            except Exception:
                pass
        if len(frames) < 2:
            return None
        df   = pd.DataFrame(frames).dropna(how="all")
        rets = df.pct_change().dropna()
        corr = rets.corr()

        with _chart_lock:
            n   = len(corr)
            fig, ax = plt.subplots(figsize=(max(5, n * 0.7 + 1), max(4, n * 0.7)))
            fig.patch.set_facecolor("#161b22")
            ax.set_facecolor("#161b22")

            data = corr.values
            im   = ax.imshow(data, cmap="RdYlGn", vmin=-1, vmax=1, aspect="auto")

            tls = list(corr.columns)
            ax.set_xticks(range(n)); ax.set_yticks(range(n))
            ax.set_xticklabels(tls, rotation=45, ha="right", fontsize=8, color="#c9d1d9")
            ax.set_yticklabels(tls, fontsize=8, color="#c9d1d9")
            for spine in ax.spines.values():
                spine.set_edgecolor("#30363d")

            for i in range(n):
                for j in range(n):
                    val = data[i, j]
                    ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                            fontsize=7, color="white" if abs(val) > 0.5 else "#c9d1d9")

            cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
            cbar.ax.tick_params(colors="#8b949e", labelsize=7)
            ax.set_title("Correlación de retornos (1 año)", color="#e6edf3", fontsize=11, pad=10)
            fig.tight_layout(pad=1.5)
            return _fig_to_response(fig)

    result = await asyncio.get_running_loop().run_in_executor(_executor, _compute_and_render)
    if result is None:
        raise HTTPException(status_code=500, detail="No hay datos suficientes")
    return result


# ── Riesgo: VaR + Monte Carlo + correlación macro ─────────────────────────────

@app.get("/riesgo", response_class=HTMLResponse)
async def riesgo_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df        = _read_csv()
    positions = {r[0]: (r[1], r[2]) for r in get_all_positions()}
    if not positions or df is None:
        return templates.TemplateResponse("riesgo.html", {
            "request": request, "has_data": False,
            "var_data": None, "macro_corr": None, "total": 0,
        })

    def _compute():
        import warnings
        warnings.filterwarnings("ignore")
        tickers  = list(positions.keys())
        values   = {}
        for t in tickers:
            row = df[df["ticker"] == t]
            if row.empty:
                continue
            p = row.iloc[0].get("price")
            if p and not _is_nan(p):
                values[t] = positions[t][0] * float(p)

        if not values:
            return None

        total = sum(values.values())
        weights = {t: v / total for t, v in values.items()}

        # Fetch 1y daily prices
        price_data = {}
        for t in values:
            try:
                hist = yf.Ticker(t).history(period="1y")["Close"]
                if len(hist) > 30:
                    price_data[t] = hist
            except Exception:
                pass

        macro_tickers = {"SPY": "S&P 500", "^VIX": "VIX", "^TNX": "Bono 10Y EE.UU."}
        for mt in macro_tickers:
            try:
                hist = yf.Ticker(mt).history(period="1y")["Close"]
                if not hist.empty:
                    price_data[mt] = hist
            except Exception:
                pass

        if len(price_data) < 2:
            return None

        df_prices  = pd.DataFrame(price_data).dropna(how="all")
        df_returns = df_prices.pct_change().dropna()

        # Portfolio daily returns (weighted)
        port_rets = pd.Series(0.0, index=df_returns.index)
        for t, w in weights.items():
            if t in df_returns.columns:
                port_rets += df_returns[t] * w

        if len(port_rets) < 20:
            return None

        # VaR (95%)
        var_95  = float(np.percentile(port_rets.dropna(), 5))
        var_99  = float(np.percentile(port_rets.dropna(), 1))
        var_eur_95 = abs(var_95) * total
        var_eur_99 = abs(var_99) * total
        vol_daily  = float(port_rets.std())
        vol_annual = vol_daily * np.sqrt(252) * 100
        mean_daily = float(port_rets.mean())

        # Correlación con macro
        macro_corr = {}
        for mt, label in macro_tickers.items():
            if mt in df_returns.columns:
                try:
                    c = float(port_rets.corr(df_returns[mt]))
                    if not np.isnan(c):
                        macro_corr[label] = round(c, 3)
                except Exception:
                    pass

        return {
            "total":       round(total, 2),
            "var_95_pct":  round(abs(var_95) * 100, 2),
            "var_99_pct":  round(abs(var_99) * 100, 2),
            "var_95_eur":  round(var_eur_95, 2),
            "var_99_eur":  round(var_eur_99, 2),
            "vol_annual":  round(vol_annual, 2),
            "mean_daily":  round(mean_daily * 100, 4),
            "macro_corr":  macro_corr,
            "port_rets":   list(port_rets.dropna().values[-252:]),
        }

    var_data = await asyncio.get_running_loop().run_in_executor(_executor, _compute)

    return templates.TemplateResponse("riesgo.html", {
        "request":  request,
        "has_data": var_data is not None,
        "var_data": var_data,
        "total":    var_data["total"] if var_data else 0,
    })


@app.get("/chart/riesgo/returns")
async def chart_riesgo_returns(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(status_code=401)

    df        = _read_csv()
    positions = {r[0]: (r[1], r[2]) for r in get_all_positions()}
    if not positions or df is None:
        raise HTTPException(status_code=400)

    def _fetch():
        values = {}
        for t in positions:
            row = df[df["ticker"] == t]
            if row.empty: continue
            p = row.iloc[0].get("price")
            if p and not _is_nan(p):
                values[t] = positions[t][0] * float(p)
        total = sum(values.values()) or 1
        weights = {t: v / total for t, v in values.items()}
        frames = {}
        for t in values:
            try:
                h = yf.Ticker(t).history(period="1y")["Close"]
                if len(h) > 30: frames[t] = h
            except Exception:
                pass
        if not frames:
            return None
        df_r = pd.DataFrame(frames).pct_change().dropna()
        port = pd.Series(0.0, index=df_r.index)
        for t, w in weights.items():
            if t in df_r.columns:
                port += df_r[t] * w
        return port.dropna()

    def _fetch_and_render():
        port_rets = _fetch()
        if port_rets is None:
            return None
        with _chart_lock:
            fig, ax = plt.subplots(figsize=(7, 3))
            fig.patch.set_facecolor("#161b22")
            ax.set_facecolor("#21262d")
            vals = port_rets.values * 100
            var95 = float(np.percentile(vals, 5))
            ax.hist(vals, bins=50, color="#1f6feb", alpha=0.7, edgecolor="none")
            ax.axvline(var95, color="#f85149", linewidth=1.5, linestyle="--",
                       label=f"VaR 95%: {var95:.2f}%")
            ax.legend(fontsize=8, labelcolor="#e6edf3", facecolor="#21262d", edgecolor="#30363d")
            ax.tick_params(colors="#8b949e", labelsize=8)
            ax.set_xlabel("Retorno diario (%)", color="#8b949e", fontsize=9)
            ax.set_ylabel("Frecuencia", color="#8b949e", fontsize=9)
            ax.set_title("Distribución de retornos diarios", color="#e6edf3", fontsize=10)
            for spine in ax.spines.values(): spine.set_edgecolor("#30363d")
            fig.tight_layout(pad=1.0)
            return _fig_to_response(fig)

    resp = await asyncio.get_running_loop().run_in_executor(_executor, _fetch_and_render)
    if resp is None:
        raise HTTPException(status_code=500)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.get("/chart/montecarlo")
async def chart_montecarlo(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(status_code=401)

    df        = _read_csv()
    positions = {r[0]: (r[1], r[2]) for r in get_all_positions()}
    if not positions or df is None:
        raise HTTPException(status_code=400)

    def _simulate():
        values = {}
        for t in positions:
            row = df[df["ticker"] == t]
            if row.empty: continue
            p = row.iloc[0].get("price")
            if p and not _is_nan(p):
                values[t] = positions[t][0] * float(p)
        total = sum(values.values()) or 1
        weights = {t: v / total for t, v in values.items()}
        frames = {}
        for t in values:
            try:
                h = yf.Ticker(t).history(period="1y")["Close"]
                if len(h) > 30: frames[t] = h
            except Exception:
                pass
        if not frames:
            return None, total
        df_r = pd.DataFrame(frames).pct_change().dropna()
        port = pd.Series(0.0, index=df_r.index)
        for t, w in weights.items():
            if t in df_r.columns:
                port += df_r[t] * w
        mu  = port.mean()
        sig = port.std()
        n_paths, n_days = 1000, 252
        rng  = np.random.default_rng(42)
        sims = rng.normal(mu, sig, (n_paths, n_days))
        paths = total * np.cumprod(1 + sims, axis=1)
        return paths, total

    def _simulate_and_render():
        paths, initial = _simulate()
        if paths is None:
            return None
        with _chart_lock:
            n_days = paths.shape[1]
            fig, ax = plt.subplots(figsize=(8, 4))
            fig.patch.set_facecolor("#161b22")
            ax.set_facecolor("#21262d")
            for p in paths[:200]:
                ax.plot(p, color="#1f6feb", alpha=0.02, linewidth=0.5)
            pct5  = np.percentile(paths, 5,  axis=0)
            pct50 = np.percentile(paths, 50, axis=0)
            pct95 = np.percentile(paths, 95, axis=0)
            x = np.arange(n_days)
            ax.fill_between(x, pct5, pct95, alpha=0.15, color="#58a6ff")
            ax.plot(x, pct50, color="#3fb950", linewidth=1.5, label="Mediana")
            ax.plot(x, pct5,  color="#f85149", linewidth=1.0, linestyle="--", label="Percentil 5%")
            ax.plot(x, pct95, color="#58a6ff", linewidth=1.0, linestyle="--", label="Percentil 95%")
            ax.axhline(initial, color="#8b949e", linewidth=0.8, linestyle=":", label="Valor actual")
            ax.legend(fontsize=8, labelcolor="#e6edf3", facecolor="#21262d", edgecolor="#30363d")
            ax.tick_params(colors="#8b949e", labelsize=8)
            ax.set_xlabel("Días de trading", color="#8b949e", fontsize=9)
            ax.set_ylabel("Valor cartera (€)", color="#8b949e", fontsize=9)
            ax.set_title("Simulación Monte Carlo — 1 año (1000 escenarios)", color="#e6edf3", fontsize=10)
            for spine in ax.spines.values(): spine.set_edgecolor("#30363d")
            fig.tight_layout(pad=1.0)
            return _fig_to_response(fig)

    resp = await asyncio.get_running_loop().run_in_executor(_executor, _simulate_and_render)
    if resp is None:
        raise HTTPException(status_code=500)
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


# ── Backtesting del score ──────────────────────────────────────────────────────

@app.get("/backtesting", response_class=HTMLResponse)
async def backtesting_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    def _compute():
        import warnings
        warnings.filterwarnings("ignore")
        # Leer historial de scores de la BD
        from database import _db
        with _db() as conn:
            rows = conn.execute("""
                SELECT ticker, date, score FROM price_history
                WHERE score IS NOT NULL AND score > 0
                ORDER BY date ASC
            """).fetchall()
        if not rows:
            return []

        # Agrupar por bucket
        results_raw = []
        price_cache = {}
        for ticker, date_str, score in rows:
            if score is None:
                continue
            if score >= 15:
                bucket = "ALTA"
            elif score >= 8:
                bucket = "MEDIA"
            else:
                bucket = "BAJA"
            if ticker not in price_cache:
                try:
                    hist = yf.Ticker(ticker).history(period="2y")["Close"]
                    price_cache[ticker] = hist
                except Exception:
                    price_cache[ticker] = None
            hist = price_cache.get(ticker)
            if hist is None or hist.empty:
                continue
            try:
                idx = pd.Timestamp(date_str).tz_localize("UTC")
                if idx not in hist.index:
                    # Find nearest
                    diffs = abs(hist.index - idx)
                    nearest = hist.index[diffs.argmin()]
                    if abs((nearest - idx).days) > 5:
                        continue
                    idx = nearest
                future_idx = idx + pd.Timedelta(days=30)
                diffs2 = abs(hist.index - future_idx)
                f_idx  = hist.index[diffs2.argmin()]
                if abs((f_idx - future_idx).days) > 10:
                    continue
                p0 = float(hist.loc[idx])
                p1 = float(hist.loc[f_idx])
                ret = (p1 - p0) / p0 * 100
                results_raw.append({"bucket": bucket, "score": score, "ret_30d": ret})
            except Exception:
                continue

        if not results_raw:
            return []

        df_bt = pd.DataFrame(results_raw)
        summary = []
        for b in ["ALTA", "MEDIA", "BAJA"]:
            sub = df_bt[df_bt["bucket"] == b]["ret_30d"]
            if len(sub) > 0:
                summary.append({
                    "bucket":   b,
                    "n":        len(sub),
                    "avg_ret":  round(float(sub.mean()), 2),
                    "med_ret":  round(float(sub.median()), 2),
                    "pct_pos":  round(float((sub > 0).mean() * 100), 1),
                    "best":     round(float(sub.max()), 2),
                    "worst":    round(float(sub.min()), 2),
                })
        return summary

    summary = await asyncio.get_running_loop().run_in_executor(_executor, _compute)
    return templates.TemplateResponse("backtesting.html", {
        "request": request, "summary": summary,
    })


# ── Exportación PDF (HTML optimizado para impresión) ──────────────────────────

@app.get("/export/pdf", response_class=HTMLResponse)
async def export_pdf(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df        = _read_csv()
    positions = {r[0]: (r[1], r[2]) for r in get_all_positions()}
    rows_data = []
    total_value = 0.0
    total_cost  = 0.0

    if df is not None:
        for _, row in df[df["category"] == "portfolio"].iterrows():
            t = row["ticker"]
            if t not in positions:
                continue
            shares, avg = positions[t]
            price = row.get("price")
            if not price or _is_nan(price):
                continue
            value   = shares * float(price)
            cost    = shares * float(avg)
            pnl_pct = (float(price) - float(avg)) / float(avg) * 100 if avg else 0
            total_value += value
            total_cost  += cost
            rows_data.append({
                "ticker": t, "name": row["name"],
                "shares": shares, "price": float(price), "avg": float(avg),
                "value": value, "pnl_pct": pnl_pct,
                "drawdown": row.get("drawdown_52w"),
                "score": row.get("score"),
            })
    rows_data.sort(key=lambda x: -x["value"])
    total_pnl = (total_value - total_cost) / total_cost * 100 if total_cost > 0 else 0

    reports = get_recent_reports(n=1)
    last_report = reports[0][2] if reports else ""

    return templates.TemplateResponse("export_pdf.html", {
        "request":     request,
        "rows":        rows_data,
        "total_value": total_value,
        "total_pnl":   total_pnl,
        "last_report": last_report,
        "generated":   datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    })


# ── Endpoints de análisis IA on-demand ────────────────────────────────────────

@app.get("/ticker/{ticker}/analizar")
async def ticker_analizar(
    ticker: str,
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    ticker = ticker.upper()
    if not _TICKER_RE.match(ticker):
        return JSONResponse({"error": "Ticker inválido"}, status_code=400)

    df      = _read_csv()
    csv_row = None
    if df is not None:
        r = df[df["ticker"] == ticker]
        if not r.empty:
            csv_row = r.iloc[0].to_dict()

    tickers_data = _load_tickers()
    notes = ""
    for cat in ("portfolio", "watchlist"):
        meta = (tickers_data.get(cat) or {}).get(ticker)
        if isinstance(meta, dict):
            notes = meta.get("notes", "") or ""
            break

    def _fetch():
        try:
            info = yf.Ticker(ticker).info or {}
        except Exception:
            info = {}
        return {
            "name":           info.get("longName") or info.get("shortName") or ticker,
            "pe_ratio":       info.get("trailingPE"),
            "pb_ratio":       info.get("priceToBook"),
            "roe":            _safe_pct(info.get("returnOnEquity")),
            "profit_margin":  _safe_pct(info.get("profitMargins")),
            "debt_equity":    info.get("debtToEquity"),
            "revenue_growth": _safe_pct(info.get("revenueGrowth")),
        }

    fundamentals = await asyncio.get_running_loop().run_in_executor(_executor, _fetch)
    text = await asyncio.get_running_loop().run_in_executor(
        _executor, explain_ticker, ticker, notes, csv_row, fundamentals
    )
    if not text:
        return JSONResponse({"error": "No se pudo generar el análisis"}, status_code=500)
    return JSONResponse({"text": text})


@app.get("/rebalanceo/sugerencia")
async def rebalanceo_sugerencia(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return JSONResponse({"error": "No autenticado"}, status_code=401)

    df        = _read_csv()
    positions = {row[0]: (row[1], row[2]) for row in get_all_positions()}
    rows_data = []

    if df is not None:
        for _, row in df[df["category"] == "portfolio"].iterrows():
            t = row["ticker"]
            if t not in positions:
                continue
            shares, _ = positions[t]
            price = row.get("price")
            if not price or _is_nan(price):
                continue
            value = shares * float(price)
            try:
                _tw_raw = row.get("target_weight")
                tw = float(_tw_raw) if _tw_raw is not None and not _is_nan(_tw_raw) else None
            except (TypeError, ValueError):
                tw = None
            rows_data.append({
                "ticker":    t,
                "name":      row["name"],
                "value":     value,
                "target_w":  tw,
                "horizon":   row.get("horizon") if row.get("horizon") and str(row.get("horizon")) != "nan" else None,
                "score":     row.get("score"),
            })

    total = sum(r["value"] for r in rows_data) if rows_data else 0.0
    for r in rows_data:
        r["current_w"] = r["value"] / total * 100 if total else 0.0
        r["diff"] = (r["current_w"] - r["target_w"]) if r["target_w"] is not None else None

    rows_data.sort(key=lambda x: -x["value"])

    text = await asyncio.get_running_loop().run_in_executor(
        _executor, suggest_rebalance, rows_data, total
    )
    if not text:
        return JSONResponse({"error": "No se pudo generar la sugerencia"}, status_code=500)
    return JSONResponse({"text": text})


@app.get("/noticias/analizar")
async def noticias_analizar(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return JSONResponse({"error": "No autenticado"}, status_code=401)

    tickers_data = _load_tickers()
    all_tickers  = []
    for cat in ("portfolio", "watchlist"):
        for t in (tickers_data.get(cat) or {}):
            all_tickers.append(t)

    def _fetch_all():
        from fetch_data import get_news, translate_headlines
        result = {}
        for t in all_tickers[:20]:
            headlines = get_news(t, n=3, translate=False)
            if headlines:
                result[t] = headlines
        # traducir en bloque
        all_hl = [h for hl in result.values() for h in hl]
        if all_hl:
            translated = translate_headlines(all_hl)
            idx = 0
            for t in result:
                n = len(result[t])
                result[t] = translated[idx:idx+n]
                idx += n
        return result

    headlines_by_ticker = await asyncio.get_running_loop().run_in_executor(_executor, _fetch_all)
    text = await asyncio.get_running_loop().run_in_executor(
        _executor, detect_news_patterns, headlines_by_ticker
    )
    if not text:
        return JSONResponse({"error": "No se pudo detectar patrones"}, status_code=500)
    return JSONResponse({"text": text})


@app.get("/operaciones/analizar")
async def operaciones_analizar(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return JSONResponse({"error": "No autenticado"}, status_code=401)

    ops = get_operations(limit=50)
    df  = _read_csv()
    current_prices: dict = {}
    if df is not None:
        for _, row in df.iterrows():
            p = row.get("price")
            if p and not _is_nan(p):
                current_prices[row["ticker"]] = float(p)

    text = await asyncio.get_running_loop().run_in_executor(
        _executor, analyze_operations, ops, current_prices
    )
    if not text:
        return JSONResponse({"error": "No se pudo generar el análisis"}, status_code=500)
    return JSONResponse({"text": text})


@app.get("/tickers/suggest-meta")
async def tickers_suggest_meta(
    ticker:  str = "",
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        return JSONResponse({}, status_code=401)
    ticker = ticker.strip().upper()
    if not _TICKER_RE.match(ticker):
        return JSONResponse({})

    def _fetch():
        try:
            return yf.Ticker(ticker).info or {}
        except Exception:
            return {}

    info   = await asyncio.get_running_loop().run_in_executor(_executor, _fetch)
    result = await asyncio.get_running_loop().run_in_executor(
        _executor, suggest_ticker_meta, ticker, info
    )
    return JSONResponse(result)


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
        try:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else
                               ["ticker", "name", "price", "drawdown_52w", "momentum_3m",
                                "score", "shares", "avg_price", "value_eur", "pnl_pct"])
            w.writeheader()
            for r in rows:
                w.writerow(r)
            yield buf.getvalue()
        finally:
            buf.close()

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
        try:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else
                               ["ticker", "name", "price", "score", "opportunity",
                                "drawdown_52w", "momentum_3m", "dividend_yield", "pe_ratio"])
            w.writeheader()
            for r in rows:
                w.writerow(r)
            yield buf.getvalue()
        finally:
            buf.close()

    return StreamingResponse(
        _gen(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=watchlist.csv"},
    )


# ── Importación masiva de tickers ─────────────────────────────────────────────

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


# ── Web Push PWA ──────────────────────────────────────────────────────────────

_SW_JS = r"""
self.addEventListener('push', function(event) {
  var data = {};
  try { data = event.data.json(); } catch(e) { data = {title:'Market Radar AI', body: event.data ? event.data.text() : ''}; }
  var title = data.title || 'Market Radar AI';
  var options = {
    body:  data.body  || '',
    icon:  data.icon  || '/icon-192.png',
    badge: '/icon-192.png',
    data:  { url: data.url || '/' },
    requireInteraction: false,
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) ? event.notification.data.url : '/';
  event.waitUntil(clients.openWindow(url));
});
"""

_ICON_PNG_CACHE: dict = {}


def _generate_icon_png(size: int) -> bytes:
    if size in _ICON_PNG_CACHE:
        return _ICON_PNG_CACHE[size]
    buf = io.BytesIO()
    with _chart_lock:
        fig, ax = plt.subplots(figsize=(size / 100, size / 100), dpi=100)
        fig.patch.set_facecolor("#161b22")
        ax.set_facecolor("#161b22")
        ax.text(0.5, 0.5, "MR", ha="center", va="center",
                fontsize=int(size * 0.35), fontweight="bold", color="#58a6ff",
                transform=ax.transAxes)
        ax.axis("off")
        try:
            fig.savefig(buf, format="png", bbox_inches="tight", facecolor="#161b22")
        finally:
            plt.close(fig)
    _ICON_PNG_CACHE[size] = buf.getvalue()
    return _ICON_PNG_CACHE[size]


@app.get("/sw.js")
async def service_worker():
    return Response(
        content=_SW_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=0", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.json")
async def pwa_manifest():
    data = {
        "name": "Market Radar AI",
        "short_name": "Market Radar",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0d1117",
        "theme_color": "#161b22",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    return Response(
        content=json.dumps(data),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/icon-192.png")
async def icon_192():
    png = await asyncio.get_running_loop().run_in_executor(_executor, _generate_icon_png, 192)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon-512.png")
async def icon_512():
    png = await asyncio.get_running_loop().run_in_executor(_executor, _generate_icon_png, 512)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/push/vapid-public-key")
async def push_vapid_public_key():
    from push_utils import get_or_create_vapid_keys
    _, pub_b64 = await asyncio.get_running_loop().run_in_executor(
        _executor, get_or_create_vapid_keys
    )
    return JSONResponse({"publicKey": pub_b64})


@app.post("/push/subscribe")
async def push_subscribe(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
    endpoint:   str = Form(...),
    p256dh:     str = Form(...),
    auth:       str = Form(...),
):
    if not _is_auth(session):
        raise HTTPException(status_code=401)
    _require_csrf(request, csrf_token)
    if not endpoint.startswith("https://"):
        raise HTTPException(status_code=400, detail="endpoint inválido")
    ua = request.headers.get("user-agent", "")[:200]
    upsert_push_subscription(endpoint, p256dh, auth, ua)
    return JSONResponse({"ok": True})


@app.post("/push/unsubscribe")
async def push_unsubscribe(
    request: Request,
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        raise HTTPException(status_code=401)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400)
    if not _validate_csrf(body.get("csrf_token", "")):
        raise HTTPException(status_code=403, detail="CSRF inválido")
    endpoint = body.get("endpoint", "")
    if endpoint:
        delete_push_subscription(endpoint)
    return JSONResponse({"ok": True})


@app.post("/push/test")
@limiter.limit("3/minute")
async def push_test(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        raise HTTPException(status_code=401)
    _require_csrf(request, csrf_token)
    from push_utils import send_push_to_all
    sent = await asyncio.get_running_loop().run_in_executor(
        _executor,
        lambda: send_push_to_all(
            "Market Radar AI",
            "Notificación de prueba. El sistema funciona correctamente.",
            "/alertas",
        ),
    )
    return JSONResponse({"sent": sent})


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8589")))
