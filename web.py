"""
Interfaz web para Market Radar AI.
Uso: uvicorn web:app --host 0.0.0.0 --port 8589
     python web.py
"""
import asyncio
import csv
import datetime
import hashlib
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
import yfinance as yf

from urllib.parse import urlparse, quote

from fastapi import Cookie, FastAPI, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import select_autoescape
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
    suggest_operation_note,
)
from database import (
    add_operation,
    get_discoveries,
    get_discoveries_generated_at,
    add_price_alert,
    count_operations,
    count_reports,
    deactivate_alert,
    delete_operation,
    delete_position,
    get_active_alerts,
    get_alert_history,
    get_all_positions,
    get_portfolio_position,
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
    get_latest_snapshot_as_df,
    get_all_tickers,
    get_tickers_as_yaml_dict,
    get_ticker_meta,
    upsert_ticker,
    update_ticker_fields,
    delete_ticker_record,
    ticker_exists,
    get_tr_isin_map,
    upsert_tr_isin,
    delete_tr_isin,
    _db,
    # ISO 27001 / 27701: auditoría, sesiones persistentes, GDPR
    log_audit_event,
    get_audit_log,
    count_audit_log,
    create_session_db,
    get_session_db,
    touch_session_db,
    delete_session_db,
    delete_expired_sessions_db,
    get_all_active_sessions_db,
    delete_all_sessions_db,
    count_active_sessions_db,
    get_oldest_session_id_db,
    purge_old_push_subscriptions,
    get_all_push_subscriptions,
    gdpr_delete_personal_data,
)
from fetch_data import get_macro_context, get_news, to_eur
from generate_csv import generate
from scoring import score_by_horizon, suggest_horizon, HORIZON_META, get_weights, _WEIGHTS

try:
    from discovery import generate_discoveries, is_stale, get_universe, refresh_universe
    _DISCOVERY_AVAILABLE = True
except Exception:
    _DISCOVERY_AVAILABLE = False
    def generate_discoveries(): return []
    def is_stale(): return True
    def get_universe(): return []
    def refresh_universe(): return []

logger = logging.getLogger("web")

# ── Config ────────────────────────────────────────────────────────────────────

CREDENTIALS_FILE      = "data/credentials.json"
INITIAL_PASSWORD_FILE = "data/initial-password.txt"
TOTP_SECRET_FILE      = "data/totp_secret.key"
DEFAULT_USERNAME      = "admin"

# Cookie secure flag: activar en producción (HTTPS) mediante COOKIE_SECURE=1
COOKIE_SECURE     = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
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
# Contadores de intentos TOTP fallidos por pending token (ISO 27001 A.9.2.2)
_totp_failed_attempts: dict = {}
_TOTP_MAX_ATTEMPTS = 3

# Límite de sesiones concurrentes (ISO 27001 A.9.2.3)
_MAX_CONCURRENT_SESSIONS = 5

# Lockout de login por IP: ip → lista de timestamps de fallos recientes
_LOCKOUT_MAX      = 5
_LOCKOUT_DURATION = 900  # 15 minutos (en segundos)
_failed_logins: dict = {}

# Lockout de login por cuenta (username): defiende contra ataques desde múltiples IPs
_ACCOUNT_LOCKOUT_MAX      = 10
_ACCOUNT_LOCKOUT_DURATION = 1800  # 30 minutos
_account_failed: dict = {}  # username → lista de timestamps

# Limpieza periódica de sesiones y tokens expirados
_last_cleanup: float = 0.0
_cleanup_lock = threading.Lock()

# Caché del CSV en memoria
_csv_cache: dict = {"df": None, "ts": 0.0}
_CSV_CACHE_TTL   = 300.0  # 5 minutos
_csv_cache_lock  = threading.RLock()

# Caché de optimización de cartera (coste alto: yfinance + scipy)
_opt_cache: dict = {"data": None, "ts": 0.0}
_OPT_CACHE_TTL  = 300.0   # 5 minutos
_opt_cache_lock = threading.RLock()

# Caché de posiciones (evita N queries idénticas a portfolio por request)
_pos_cache: dict = {"data": None, "ts": 0.0}
_POS_CACHE_TTL  = 60.0    # 1 minuto
_pos_cache_lock = threading.RLock()

# Caché de score_by_horizon ligado al TTL del CSV
_scored_cache: dict = {"df": None, "csv_ts": 0.0}
_scored_cache_lock = threading.RLock()

# Caché de históricos yfinance para gráficos (evita re-descargar en cada request)
_hist_cache: dict = {}   # {ticker: (df, monotonic_ts)}
_HIST_CACHE_TTL = 3600.0  # 1 hora
_hist_cache_lock = threading.RLock()


def _get_positions():
    """get_all_positions() con caché de 60 s para evitar N queries idénticas."""
    import time as _t
    with _pos_cache_lock:
        if _pos_cache["data"] is not None and _t.monotonic() - _pos_cache["ts"] < _POS_CACHE_TTL:
            return _pos_cache["data"]
    data = get_all_positions()
    with _pos_cache_lock:
        _pos_cache["data"] = data
        _pos_cache["ts"] = _t.monotonic()
    return data


def _invalidate_positions_cache():
    with _pos_cache_lock:
        _pos_cache["data"] = None


def _get_scored_df(df):
    """score_by_horizon(df) con caché vinculado al TTL del CSV (misma vida útil)."""
    import time as _t
    with _scored_cache_lock:
        if _scored_cache["df"] is not None and _scored_cache["csv_ts"] == _csv_cache["ts"]:
            return _scored_cache["df"]
    scored = score_by_horizon(df)
    with _scored_cache_lock:
        _scored_cache["df"] = scored
        _scored_cache["csv_ts"] = _csv_cache["ts"]
    return scored


def _get_ticker_hist(ticker: str, period: str = "1y"):
    """yf.Ticker().history() con caché en memoria de 1 h por ticker.

    Double-checked locking: evita descargas duplicadas cuando dos threads
    piden el mismo ticker con caché expirado simultáneamente.
    """
    import time as _t
    key = f"{ticker}:{period}"
    with _hist_cache_lock:
        entry = _hist_cache.get(key)
        if entry is not None and _t.monotonic() - entry[1] < _HIST_CACHE_TTL:
            return entry[0]
    # Fetch fuera del lock para no bloquear otros tickers,
    # pero re-verificamos dentro antes de escribir (double-check).
    hist = yf.Ticker(ticker).history(period=period)
    with _hist_cache_lock:
        entry = _hist_cache.get(key)
        if entry is None or _t.monotonic() - entry[1] >= _HIST_CACHE_TTL:
            _hist_cache[key] = (hist, _t.monotonic())
        else:
            hist = entry[0]  # otro thread ya lo actualizó, usar el suyo
    return hist


# ── Credential helpers ────────────────────────────────────────────────────────

_USERNAME_RE = re.compile(r'^[a-zA-Z0-9_\-\.]{3,32}$')
_TICKER_RE   = re.compile(r'^[A-Z0-9.\-]{1,12}$')

# Longitudes máximas de campos de formulario (ISO 27001 A.14.2 — secure coding)
_MAX_NAME_LEN   = 100
_MAX_NOTES_LEN  = 500
_MAX_BLOCK_LEN  = 80
_MAX_REGION_LEN = 80


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
    # ISO 27001 A.9.4.3 — complejidad: al menos un carácter especial
    if not re.search(r'[^A-Za-z0-9]', password):
        return "La contraseña debe contener al menos un carácter especial (!@#$%^&* etc.)."
    return None


def _load_credentials() -> dict:
    try:
        # Validar permisos del fichero de credenciales (ISO 27001 A.10)
        try:
            mode = oct(os.stat(CREDENTIALS_FILE).st_mode)[-3:]
            if mode != "600":
                logger.warning(
                    "Permisos inseguros en %s: %s (debería ser 600). Corrigiendo...",
                    CREDENTIALS_FILE, mode
                )
                os.chmod(CREDENTIALS_FILE, 0o600)
        except OSError:
            pass
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
        logger.warning("Credenciales iniciales escritas. Accede al dashboard para completar la configuración.")
        return creds


def _save_credentials(username: str, password: str, first_login: bool = False) -> None:
    import datetime as _dt
    creds = {
        "username": username,
        "password_hash": _hash_password(password),
        "first_login": first_login,
        "password_changed_at": _dt.datetime.now().isoformat(timespec="seconds"),
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


# Días antes de que expire la contraseña (ISO 27001 A.9.2.1)
_PASSWORD_EXPIRY_DAYS = 90


def _is_password_expired(creds: dict) -> bool:
    """Devuelve True si la contraseña lleva más de _PASSWORD_EXPIRY_DAYS días sin cambiar."""
    import datetime as _dt
    changed_at_str = creds.get("password_changed_at")
    if not changed_at_str:
        return False  # Credenciales antiguas sin timestamp → no forzar aún
    try:
        changed_at = _dt.datetime.fromisoformat(changed_at_str)
        age_days = (_dt.datetime.now() - changed_at).days
        return age_days >= _PASSWORD_EXPIRY_DAYS
    except Exception:
        return False


def _password_days_remaining(creds: dict) -> Optional[int]:
    """Devuelve los días que quedan antes de que expire la contraseña, o None."""
    import datetime as _dt
    changed_at_str = creds.get("password_changed_at")
    if not changed_at_str:
        return None
    try:
        changed_at = _dt.datetime.fromisoformat(changed_at_str)
        age_days = (_dt.datetime.now() - changed_at).days
        remaining = _PASSWORD_EXPIRY_DAYS - age_days
        return max(0, remaining)
    except Exception:
        return None


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

_TOTP_B32_RE = re.compile(r'^[A-Z2-7]{16,64}$')

def _totp_secret() -> Optional[str]:
    """Carga y valida el TOTP secret (ISO 27001 A.10.1.1 — integridad de material criptográfico)."""
    try:
        s = open(TOTP_SECRET_FILE).read().strip().upper()
        if not s:
            return None
        if not _TOTP_B32_RE.match(s):
            # Fichero corrompido o manipulado — logar y tratar como 2FA deshabilitado
            logger.critical("TOTP secret inválido o corrompido en %s — 2FA tratado como deshabilitado", TOTP_SECRET_FILE)
            try:
                log_audit_event("totp_integrity_error", details=f"file={TOTP_SECRET_FILE},len={len(s)}")
            except Exception:
                pass
            return None
        return s
    except FileNotFoundError:
        return None


def _totp_enabled() -> bool:
    return bool(_totp_secret())


def _verify_totp(code: str) -> bool:
    secret = _totp_secret()
    if not secret:
        return True
    code = code.strip()
    # ISO 27001 A.9.4 — validar formato antes de verificar (previene timing attacks con entradas anómalas)
    if not code.isdigit() or len(code) > 10:
        return False
    return pyotp.TOTP(secret).verify(code, valid_window=2)


limiter      = Limiter(key_func=get_remote_address)
app          = FastAPI(title="Market Radar AI", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


async def _generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """ISO 27001 A.5 — Evita filtración de stack traces al cliente (OWASP A05)."""
    logger.error("Excepción no capturada en %s: %s", request.url.path, type(exc).__name__, exc_info=exc)
    try:
        ip = get_remote_address(request)
        log_audit_event("unhandled_exception", ip_address=ip, details=f"path={request.url.path},type={type(exc).__name__}")
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"detail": "Error interno del servidor"})


app.add_exception_handler(Exception, _generic_exception_handler)
init_db()  # Asegura migraciones de BD tanto en uvicorn directo como vía __main__
# Ficheros estáticos (Alpine.js auto-alojado, etc.)
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
# ISO 27001 A.14.2 — auto-escaping explícito; no depende de defaults del framework (XSS prevention)
templates    = Jinja2Templates(
    directory="templates",
    autoescape=select_autoescape(enabled_extensions=("html", "xml")),
)
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


def _safe_float(v, default=None):
    """Convierte v a float; devuelve default si falla o es NaN."""
    try:
        f = float(v)
        return f if not math.isnan(f) else default
    except (TypeError, ValueError):
        return default


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
    # Inline formatting
    text = re.sub(r'`([^`\n]+)`',    r'<code class="tg-code">\1</code>', text)
    text = re.sub(r'\*\*([^\*\n]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*([^\*\n]+)\*', r'<strong>\1</strong>', text)
    text = re.sub(r'_([^_\n]+)_',    r'<em>\1</em>', text)
    # Section titles: lines that start with an emoji or are ALL-CAPS (≥4 chars, no digits)
    _SECTION_RE = re.compile(
        r'^([\U0001F300-\U0001FFFF\U00002600-\U000027BF].*|[A-ZÁÉÍÓÚÑ\s]{4,})$',
        re.MULTILINE | re.UNICODE,
    )
    lines = text.split('\n')
    out = []
    buf = []  # lines accumulating into a paragraph

    def flush():
        if buf:
            content = ' '.join(l for l in buf if l)
            if content:
                out.append(f'<p>{content}</p>')
            buf.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
        elif _SECTION_RE.match(stripped):
            flush()
            out.append(f'<div class="report-section-title">{stripped}</div>')
        else:
            buf.append(stripped)
    flush()
    return Markup(''.join(out))


def _tojson_filter(v) -> str:
    """Serializa un valor Python a JSON seguro para uso en atributos HTML."""
    return json.dumps(v, ensure_ascii=False)

templates.env.filters.update({
    "eur":       fmt_eur,
    "pct":       fmt_pct,
    "num":       fmt_num,
    "dd_class":  dd_class,
    "pnl_class": pnl_class,
    "opp_class": opp_class,
    "tg":        tg_to_html,
    "tojson":    _tojson_filter,
})
# CSRF token disponible en todos los templates como {{ csrf_token }}
templates.env.globals["csrf_token"] = _csrf_state["current"]


@app.middleware("http")
async def _refresh_csrf_global(request: Request, call_next):
    """Rota el CSRF token si ha expirado; añade cabeceras de seguridad HTTP."""
    _rotate_csrf_if_needed()
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if "text/html" in ct:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    # Cabeceras de seguridad en todas las respuestas
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # HSTS: solo cuando el servicio está detrás de HTTPS (COOKIE_SECURE=1)
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    # Bloquea políticas cross-domain de Flash/Silverlight (OWASP A05)
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    # Previene leaks de memoria entre ventanas/tabs (COOP)
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    # CSP: permite recursos propios + inline styles/scripts necesarios por Alpine.js
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none';"
    )
    return response


# ── Auth ──────────────────────────────────────────────────────────────────────

def _create_session(ip: Optional[str] = None, user_agent: Optional[str] = None) -> str:
    """Crea sesión en memoria Y en BD (persistente entre reinicios).
    Si se supera _MAX_CONCURRENT_SESSIONS, invalida la sesión más antigua (ISO 27001 A.9.2.3)."""
    import datetime as _dt
    # Rotar sesión más antigua si se supera el límite de concurrencia
    try:
        if count_active_sessions_db() >= _MAX_CONCURRENT_SESSIONS:
            oldest = get_oldest_session_id_db()
            if oldest:
                _active_sessions.pop(oldest, None)
                delete_session_db(oldest)
                logger.info("Sesión más antigua rotada por límite de concurrencia (max=%d)", _MAX_CONCURRENT_SESSIONS)
                try:
                    log_audit_event(
                        "session_rotated_max_concurrent",
                        ip_address=ip,
                        details=f"max={_MAX_CONCURRENT_SESSIONS}",
                    )
                except Exception:
                    pass
    except Exception:
        pass
    sid = secrets.token_urlsafe(32)
    _active_sessions[sid] = _time.monotonic() + SESSION_EXPIRY
    # Persistir en BD con timestamp absoluto (ISO 8601)
    expires_abs = (_dt.datetime.now() + _dt.timedelta(seconds=SESSION_EXPIRY)).isoformat(timespec="seconds")
    try:
        create_session_db(sid, expires_abs, ip_address=ip, user_agent=user_agent)
    except Exception:
        logger.exception("Error persistiendo sesión en BD")
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
        if session in _active_sessions:
            if now > _active_sessions[session]:
                del _active_sessions[session]
                try:
                    delete_session_db(session)
                except Exception:
                    pass
                return False
            try:
                touch_session_db(session)  # Actualiza last_seen para monitoreo de sesiones (A.12.4)
            except Exception:
                pass
            return True
    # No está en memoria → buscar en BD (sesión restaurada tras reinicio)
    try:
        row = get_session_db(session)
        if row:
            # Restaurar en memoria para próximas peticiones
            import datetime as _dt
            expires_abs = _dt.datetime.fromisoformat(row[1])
            remaining = (expires_abs - _dt.datetime.now()).total_seconds()
            if remaining > 0:
                with _cleanup_lock:
                    _active_sessions[session] = _time.monotonic() + remaining
                try:
                    touch_session_db(session)
                except Exception:
                    pass
                return True
    except Exception:
        pass
    return False

def _invalidate_session(session: Optional[str]) -> None:
    if session:
        _active_sessions.pop(session, None)
        try:
            delete_session_db(session)
        except Exception:
            pass

def _cleanup_expired_state() -> None:
    """Elimina sesiones, tokens y registros de bloqueo expirados para evitar crecimiento ilimitado."""
    now = _time.monotonic()
    expired_sessions = [sid for sid, exp in list(_active_sessions.items()) if now > exp]
    for sid in expired_sessions:
        _active_sessions.pop(sid, None)
    expired_tokens = [tok for tok, exp in list(_pending_tokens.items()) if now > exp]
    for tok in expired_tokens:
        _pending_tokens.pop(tok, None)
        _totp_failed_attempts.pop(tok, None)  # limpia contadores asociados
    # Limpiar IPs sin intentos fallidos activos (ISO 27001 A.12 — prevenir memory leak)
    expired_ips = [
        ip for ip, attempts in list(_failed_logins.items())
        if not any(now - t < _LOCKOUT_DURATION for t in attempts)
    ]
    for ip in expired_ips:
        _failed_logins.pop(ip, None)
    # Limpiar usernames sin intentos fallidos activos
    expired_users = [
        u for u, attempts in list(_account_failed.items())
        if not any(now - t < _ACCOUNT_LOCKOUT_DURATION for t in attempts)
    ]
    for u in expired_users:
        _account_failed.pop(u, None)
    # Limpiar sesiones expiradas de BD (throttleado — ya viene del lock de 60s)
    try:
        delete_expired_sessions_db()
    except Exception:
        pass


def _load_sessions_from_db() -> None:
    """Carga sesiones activas de BD a memoria al arrancar (restaura tras reinicio)."""
    try:
        rows = get_all_active_sessions_db()
        import datetime as _dt
        for sid, expires_at_str in rows:
            expires_abs = _dt.datetime.fromisoformat(expires_at_str)
            remaining = (expires_abs - _dt.datetime.now()).total_seconds()
            if remaining > 0:
                _active_sessions[sid] = _time.monotonic() + remaining
        if rows:
            logger.info("Sesiones restauradas desde BD: %d", len(rows))
            try:
                log_audit_event("sessions_restored_from_db", details=f"count={len(rows)}")
            except Exception:
                pass
    except Exception:
        logger.exception("Error cargando sesiones desde BD")

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

def _check_account_lockout(username: str) -> bool:
    """Devuelve True si la cuenta está bloqueada por exceso de intentos fallidos (multi-IP)."""
    now = _time.monotonic()
    attempts = [t for t in _account_failed.get(username, []) if now - t < _ACCOUNT_LOCKOUT_DURATION]
    _account_failed[username] = attempts
    return len(attempts) >= _ACCOUNT_LOCKOUT_MAX

def _record_account_failed(username: str) -> None:
    _account_failed.setdefault(username, []).append(_time.monotonic())

def _reset_account_lockout(username: str) -> None:
    _account_failed.pop(username, None)

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
        logger.warning("CSRF inválido: ip=%s path=%s", get_remote_address(request), request.url.path)
        raise HTTPException(403, "Token CSRF inválido")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_tickers() -> dict:
    """Carga la configuración de tickers desde la BD."""
    return get_tickers_as_yaml_dict()


def _save_tickers(data: dict) -> None:
    """Guarda la configuración de tickers en la BD."""
    for cat in ("portfolio", "watchlist"):
        for ticker, meta in (data.get(cat) or {}).items():
            if not isinstance(meta, dict):
                meta = {}
            upsert_ticker(
                ticker=ticker, category=cat,
                name=meta.get("name"), block=meta.get("block"),
                region=meta.get("region"), horizon=meta.get("horizon"),
                target_weight=meta.get("target_weight"),
                target_price=meta.get("target_price"),
                notes=meta.get("notes"),
            )
    for isin, t in (data.get("tr_isin_map") or {}).items():
        upsert_tr_isin(isin, t)


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
        df = get_latest_snapshot_as_df()
        _csv_cache["df"] = df
        _csv_cache["ts"] = now
        return df

def _invalidate_csv_cache() -> None:
    with _csv_cache_lock:
        _csv_cache["df"] = None
        _csv_cache["ts"] = 0.0
    with _opt_cache_lock:
        _opt_cache["data"] = None
        _opt_cache["ts"]   = 0.0
    with _scored_cache_lock:
        _scored_cache["df"]     = None
        _scored_cache["csv_ts"] = 0.0
    with _hist_cache_lock:
        _hist_cache.clear()


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
      not_configured — faltan TR_PHONE / TR_PIN (BD o env)
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
    df = score_by_horizon(df)
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
        positions = _get_positions()
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
                    headers={"Cache-Control": "private, max-age=300"})


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


# ── Upcoming events (ex-div + earnings) ────────────────────────────────────────

@app.get("/api/upcoming-events")
@limiter.limit("60/minute")
async def upcoming_events(request: Request, session: Optional[str] = Cookie(default=None)):
    """Devuelve ex-dividendos y earnings próximos en los próximos 7 días (desde caché BD)."""
    if not _is_auth(session):
        raise HTTPException(401)

    exdiv_list = []
    earnings_list = []
    try:
        cached_exdiv = get_setting("upcoming_exdiv_cache")
        if cached_exdiv:
            exdiv_list = json.loads(cached_exdiv)
    except Exception:
        pass
    try:
        cached_earnings = get_setting("upcoming_earnings_cache")
        if cached_earnings:
            earnings_list = json.loads(cached_earnings)
    except Exception:
        pass

    exdiv_list.sort(key=lambda x: x.get("date", ""))
    earnings_list.sort(key=lambda x: x.get("date", ""))
    return JSONResponse({"exdiv": exdiv_list, "earnings": earnings_list})


# ── Chart endpoints ───────────────────────────────────────────────────────────

@app.get("/chart/precio/{ticker}")
@limiter.limit("20/minute")
async def chart_precio(request: Request, ticker: str, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)
    ticker = ticker.upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(400, "Ticker inválido")

    def _fetch():
        try:
            return _get_ticker_hist(ticker, period="1y")
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
@limiter.limit("20/minute")
async def chart_historial(request: Request, ticker: str, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)
    ticker = ticker.upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(400, "Ticker inválido")
    rows = get_ticker_history(ticker, days=30)
    if len(rows) < 2:
        raise HTTPException(404, "Sin historial suficiente")
    fig = _make_history_chart(ticker.upper(), rows)
    if fig is None:
        raise HTTPException(404)
    return _fig_to_response(fig)


@app.get("/chart/valor-cartera")
@limiter.limit("20/minute")
async def chart_valor_cartera(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)
    rows = get_portfolio_value_history(days=365)
    if len(rows) < 2:
        raise HTTPException(404, "Sin historial suficiente")

    def _make():
        with _chart_lock:
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

    fig = await asyncio.get_running_loop().run_in_executor(_executor, _make)
    return _fig_to_response(fig)


@app.get("/cartera/valor-historico")
@limiter.limit("30/minute")
async def valor_historico(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)
    rows = get_portfolio_value_history(days=90)
    return JSONResponse([{"date": r[0], "total": r[1]} for r in rows])


@app.get("/chart/benchmark")
@limiter.limit("20/minute")
async def chart_benchmark(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(401)

    rows = get_portfolio_value_history(days=365)
    if len(rows) < 5:
        raise HTTPException(404, "Sin historial suficiente (se necesitan al menos 5 días)")

    start_date = rows[0][0]

    custom_ticker = (get_setting("benchmark_ticker") or "").strip().upper() or None

    def _make():
        try:
            spy_full = _get_ticker_hist("SPY", period="2y")["Close"]
            spy = spy_full[spy_full.index >= pd.Timestamp(start_date, tz="UTC")]
        except Exception:
            spy = pd.Series(dtype=float)
        try:
            ewq_full = _get_ticker_hist("EWQ", period="2y")["Close"]
            ewq = ewq_full[ewq_full.index >= pd.Timestamp(start_date, tz="UTC")]
        except Exception:
            ewq = pd.Series(dtype=float)

        custom_series = pd.Series(dtype=float)
        if custom_ticker:
            try:
                ct_full = _get_ticker_hist(custom_ticker, period="2y")["Close"]
                custom_series = ct_full[ct_full.index >= pd.Timestamp(start_date, tz="UTC")]
            except Exception:
                custom_series = pd.Series(dtype=float)

        dates_pf = pd.to_datetime([r[0] for r in rows])
        values_pf = [r[1] for r in rows]

        # Normalize to 100 at start
        base_pf = values_pf[0]
        norm_pf = [v / base_pf * 100 for v in values_pf]

        with _chart_lock:
            fig, ax = plt.subplots(figsize=(9, 4))
            _style_ax(ax, fig)

            ax.plot(dates_pf, norm_pf, color=_C_BLUE, linewidth=2, label="Mi cartera", zorder=3)

            if not spy.empty:
                spy_norm = spy / spy.iloc[0] * 100
                ax.plot(spy.index, spy_norm.values, color=_C_GREEN, linewidth=1.2, linestyle="--", label="SPY (S&P500)", alpha=0.8)

            if not ewq.empty:
                ewq_norm = ewq / ewq.iloc[0] * 100
                ax.plot(ewq.index, ewq_norm.values, color="#d29922", linewidth=1.2, linestyle="--", label="EWQ (Euro Stoxx)", alpha=0.8)

            if not custom_series.empty and custom_ticker:
                ct_norm = custom_series / custom_series.iloc[0] * 100
                ax.plot(custom_series.index, ct_norm.values, color="#da7adf", linewidth=1.2, linestyle="--", label=custom_ticker, alpha=0.8)

            ax.axhline(100, color=_C_TEXT, linewidth=0.6, linestyle=":")
            ax.set_title(f"Comparativa vs benchmark (base 100 desde {start_date})", fontsize=11, pad=8)
            ax.set_ylabel("Rendimiento (base 100)", fontsize=9)
            ax.legend(fontsize=8, facecolor=_C_CARD, edgecolor=_C_GRID, labelcolor=_C_FG)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
            ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            fig.autofmt_xdate(rotation=30)
            fig.tight_layout()
            return fig

    fig = await asyncio.get_running_loop().run_in_executor(_executor, _make)
    return _fig_to_response(fig)


# ── QR code ───────────────────────────────────────────────────────────────────

_SVG_DANGEROUS_RE = re.compile(r"<script|javascript:|on\w+=", re.IGNORECASE)

def _make_qr_svg(uri: str) -> str:
    """Genera QR SVG. Valida que el SVG no contiene scripts (ISO 27001 A.14.2)."""
    buf = io.BytesIO()
    try:
        segno.make_qr(uri).save(buf, kind="svg", scale=4, border=1, xmldecl=False, nl=False)
        svg = buf.getvalue().decode("utf-8")
    finally:
        buf.close()
    if _SVG_DANGEROUS_RE.search(svg):
        logger.critical("QR SVG contiene contenido potencialmente peligroso — rechazado")
        return ""
    return svg



# ── Privacidad y GDPR (ISO 27701) ─────────────────────────────────────────────

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("privacy.html", {"request": request})


@app.get("/gdpr", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def gdpr_page(request: Request, session: Optional[str] = Cookie(default=None), deleted: str = ""):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("gdpr.html", {"request": request, "deleted": deleted})


@app.get("/gdpr/export")
@limiter.limit("2/minute")
async def gdpr_export(request: Request, session: Optional[str] = Cookie(default=None)):
    """Descarga todos los datos personales como JSON (ISO 27701 Art. 9 - portabilidad)."""
    if not _is_auth(session):
        raise HTTPException(401)
    ip = get_remote_address(request)
    import datetime as _dt

    positions  = get_all_positions()
    operations = get_operations(limit=10000, order_asc=True)
    alerts     = get_active_alerts()
    tickers    = get_all_tickers()

    export = {
        "exported_at":   _dt.datetime.now().isoformat(timespec="seconds"),
        "app":           "Market Radar AI",
        "gdpr_basis":    "ISO 27701 Art. 9 — Derecho de portabilidad",
        "portfolio": [
            {"ticker": r[0], "shares": r[1], "avg_price_eur": r[2]}
            for r in positions
        ],
        "operations": [
            {"id": r[0], "ticker": r[1], "date": r[2], "type": r[3],
             "shares": r[4], "price_eur": r[5], "notes": r[6], "commission_eur": r[7]}
            for r in operations
        ],
        "alerts": [
            {"id": r[0], "ticker": r[1], "target_price": r[2], "direction": r[3],
             "created": r[4], "condition_type": r[5], "condition_value": r[6], "expires_at": r[7]}
            for r in alerts
        ],
        "tickers": tickers,
        "push_subscriptions": [
            {
                "endpoint_hash": hashlib.sha256(r[0].encode()).hexdigest()[:16],
                "user_agent": r[3] if len(r) > 3 else None,
                "created": r[4] if len(r) > 4 else None,
            }
            for r in get_all_push_subscriptions()
        ],
        # ISO 27701 Art. 9 / RGPD Art. 20 — los eventos de auditoría son datos personales
        "audit_log": [
            {"event_type": row[0], "ip_address": row[1], "details": row[2], "created_at": row[3]}
            for row in get_audit_log(limit=10000)
        ],
    }

    log_audit_event("gdpr_export", ip_address=ip, details="full_data_export")
    content = json.dumps(export, ensure_ascii=False, indent=2, default=str)
    filename = f"market-radar-export-{_dt.date.today().isoformat()}.json"
    return Response(
        content=content.encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


@app.post("/gdpr/delete")
@limiter.limit("1/minute")
async def gdpr_delete(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
    confirm: str = Form(default=""),
):
    """Elimina todos los datos personales (ISO 27701 Art. 9 - derecho al olvido)."""
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    if confirm != "BORRAR":
        return RedirectResponse("/gdpr?error=confirm", status_code=303)
    ip = get_remote_address(request)
    counts = gdpr_delete_personal_data()
    log_audit_event("gdpr_delete", ip_address=ip, details=json.dumps(counts))
    # Invalidar sesión actual (los datos han sido purgados)
    _invalidate_session(session)
    resp = RedirectResponse("/gdpr?deleted=1", status_code=303)
    resp.delete_cookie("session")
    return resp


# ── Audit log (ISO 27001 A.12.4.1) ───────────────────────────────────────────

@app.get("/audit-log", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def audit_log_page(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    page: int = 1,
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    per_page = 50
    offset   = (page - 1) * per_page
    events   = get_audit_log(limit=per_page, offset=offset)
    total    = count_audit_log()
    pages    = max(1, (total + per_page - 1) // per_page)
    return templates.TemplateResponse("audit_log.html", {
        "request": request,
        "events":  events,
        "page":    page,
        "pages":   pages,
        "total":   total,
    })


# ── Health ────────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def _on_startup():
    """Restaura sesiones activas desde BD tras reinicio (ISO 27001 A.9.4)."""
    _load_sessions_from_db()


@app.on_event("shutdown")
async def _on_shutdown():
    """ISO 27001 A.12 — cierre ordenado del executor; evita corrupción de datos en tareas pendientes."""
    _executor.shutdown(wait=True)


@app.get("/health")
async def health():
    # ISO 27001 A.5: no exponer detalles internos en endpoint público (sin auth)
    try:
        with _db() as conn:
            conn.execute("SELECT 1")
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        logger.error("Health check DB error: %s", exc)
        return JSONResponse({"status": "db_error"}, status_code=503)


# ── Login ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
@limiter.limit("3/minute")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    ip = get_remote_address(request)
    ua = request.headers.get("user-agent", "")
    _uname_hash = hashlib.sha256(username.encode()).hexdigest()[:16]
    if _check_lockout(ip):
        log_audit_event("login_locked", ip_address=ip, details=f"reason=ip_lockout,uname_hash={_uname_hash}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "ip_locked",
        }, status_code=429)
    if _check_account_lockout(username):
        log_audit_event("login_locked", ip_address=ip, details=f"reason=account_lockout,uname_hash={_uname_hash}")
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "account_locked",
        }, status_code=429)

    creds = _load_credentials()
    if username != creds["username"] or not _verify_password(password, creds["password_hash"]):
        _record_failed_login(ip)
        _record_account_failed(username)
        log_audit_event("login_failed", ip_address=ip, details=f"uname_hash={_uname_hash}")
        return RedirectResponse("/login?error=1", status_code=303)

    _reset_lockout(ip)
    _reset_account_lockout(username)
    _uname_hash_ok = hashlib.sha256(creds['username'].encode()).hexdigest()[:16]
    log_audit_event("login_success", ip_address=ip, details=f"uname_hash={_uname_hash_ok}")

    # Primer login: forzar configuración
    if creds.get("first_login"):
        token = _create_pending_token(600)
        resp = RedirectResponse("/setup/first-login", status_code=303)
        resp.set_cookie("setup_pending", token, httponly=True, samesite="strict", max_age=600, secure=COOKIE_SECURE)
        return resp

    # Contraseña expirada → forzar cambio
    if _is_password_expired(creds):
        _uname_hash_exp = hashlib.sha256(creds['username'].encode()).hexdigest()[:16]
        log_audit_event("password_expired", ip_address=ip, details=f"uname_hash={_uname_hash_exp}")
        token = _create_pending_token(600)
        resp = RedirectResponse("/settings/credentials?expired=1", status_code=303)
        resp.set_cookie("pwd_expired_token", token, httponly=True, samesite="strict", max_age=600, secure=COOKIE_SECURE)
        return resp

    # 2FA activo
    if _totp_enabled():
        token = _create_pending_token(300)
        resp = RedirectResponse("/login/totp", status_code=303)
        resp.set_cookie("totp_pending", token, httponly=True, samesite="strict", max_age=300, secure=COOKIE_SECURE)
        return resp

    sid = _create_session(ip=ip, user_agent=ua)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", sid, httponly=True, samesite="strict", max_age=SESSION_EXPIRY, secure=COOKIE_SECURE)
    return resp


@app.get("/login/totp", response_class=HTMLResponse)
async def totp_page(request: Request, totp_pending: Optional[str] = Cookie(default=None), error: str = ""):
    if not totp_pending or totp_pending not in _pending_tokens:
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("login_totp.html", {"request": request, "error": error})


@app.post("/login/totp")
@limiter.limit("3/minute")
async def totp_verify(
    request: Request,
    code: str = Form(...),
    totp_pending: Optional[str] = Cookie(default=None),
):
    ip = get_remote_address(request)
    ua = request.headers.get("user-agent", "")
    if not _consume_pending_token(totp_pending):
        return RedirectResponse("/login", status_code=303)
    if not _verify_totp(code):
        log_audit_event("totp_failed", ip_address=ip)
        # Contar intentos fallidos (ISO 27001 A.9.2.2 — protección fuerza bruta TOTP)
        prev_count = _totp_failed_attempts.pop(totp_pending, 0)
        new_count  = prev_count + 1
        if new_count >= _TOTP_MAX_ATTEMPTS:
            log_audit_event("totp_brute_force", ip_address=ip, details="max_totp_attempts_exceeded")
            return RedirectResponse("/login?error=totp_locked", status_code=303)
        new_token = _create_pending_token(300)
        _totp_failed_attempts[new_token] = new_count
        resp = templates.TemplateResponse(
            "login_totp.html", {"request": request, "error": "1"}, status_code=200
        )
        resp.set_cookie("totp_pending", new_token, httponly=True, samesite="strict", max_age=300, secure=COOKIE_SECURE)
        return resp
    log_audit_event("totp_success", ip_address=ip)
    sid = _create_session(ip=ip, user_agent=ua)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("session", sid, httponly=True, samesite="strict", max_age=SESSION_EXPIRY, secure=COOKIE_SECURE)
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
@limiter.limit("3/minute")
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
    _tc = totp_code.strip()
    # ISO 27001 A.9.4 — validar formato antes de verificar (consistente con _verify_totp)
    if not _tc.isdigit() or len(_tc) > 10 or not pyotp.TOTP(totp_secret).verify(_tc, valid_window=2):
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
        resp.set_cookie("setup_pending", new_token, httponly=True, samesite="strict", max_age=600, secure=COOKIE_SECURE)
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
@limiter.limit("5/minute")
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
    log_audit_event("totp_enabled", ip_address=get_remote_address(request))
    return RedirectResponse("/2fa/setup?ok=1", status_code=303)


@app.post("/2fa/disable")
@limiter.limit("3/minute")
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
    log_audit_event("totp_disabled", ip_address=get_remote_address(request))
    return RedirectResponse("/2fa/setup?disabled=1", status_code=303)


# ── Cambiar credenciales ───────────────────────────────────────────────────────

@app.get("/settings/credentials", response_class=HTMLResponse)
async def credentials_page(
    request: Request,
    session: Optional[str] = Cookie(default=None),
    ok: str = "",
    expired: str = "",
):
    # Permitir acceso si hay token de contraseña expirada (flujo post-login)
    pwd_expired_token = request.cookies.get("pwd_expired_token")
    if not _is_auth(session) and not (expired and pwd_expired_token and pwd_expired_token in _pending_tokens):
        return RedirectResponse("/login", status_code=302)
    creds = _load_credentials()
    days_remaining = _password_days_remaining(creds)
    return templates.TemplateResponse("settings_credentials.html", {
        "request": request,
        "current_username": creds["username"],
        "ok": ok,
        "expired": expired,
        "days_remaining": days_remaining,
        "expiry_days": _PASSWORD_EXPIRY_DAYS,
    })


@app.post("/settings/credentials")
@limiter.limit("3/minute")  # ISO 27001 A.9.4 — protección fuerza bruta cambio credenciales
async def credentials_update(
    request: Request,
    username: str = Form(...),
    current_password: str = Form(...),
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
    # Verificar contraseña actual antes de permitir el cambio (ISO 27001 A.9.2.1)
    if not _verify_password(current_password, creds["password_hash"]):
        ip = get_remote_address(request)
        log_audit_event("credentials_change_rejected", ip_address=ip, details="wrong_current_password")
        errors.append("Contraseña actual incorrecta.")
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
    ip = get_remote_address(request)
    ua = request.headers.get("user-agent", "")
    _uname_hash_chg = hashlib.sha256(username.strip().encode()).hexdigest()[:16]
    log_audit_event("credentials_changed", ip_address=ip, details=f"uname_hash={_uname_hash_chg}")
    # Invalidar TODAS las sesiones activas para prevenir acceso con credenciales antiguas (ISO 27001 A.9.2.6)
    delete_all_sessions_db()
    _active_sessions.clear()
    new_sid = _create_session(ip=ip, user_agent=ua)
    resp = RedirectResponse("/settings/credentials?ok=1", status_code=303)
    resp.set_cookie("session", new_sid, httponly=True, samesite="strict", max_age=SESSION_EXPIRY, secure=COOKIE_SECURE)
    return resp


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
        "type": "password",
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
    {
        "key": "COMMISSION_EUR",
        "label": "Comisión por operación (€)",
        "hint": "Coste en euros de cada operación de compra o venta. Por defecto 1 €.",
        "section": "Operaciones",
        "type": "number",
        "default": "1.0",
        "restart": False,
    },
    # Scoring weights — largo
    {"key": "SCORE_LARGO_DRAWDOWN",  "label": "Largo · Drawdown 52w",   "section": "Scoring — Pesos", "type": "number", "default": "0.20", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_LARGO_MOM3M",     "label": "Largo · Momentum 3m",    "section": "Scoring — Pesos", "type": "number", "default": "0.05", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_LARGO_VOL",       "label": "Largo · Volatilidad",     "section": "Scoring — Pesos", "type": "number", "default": "0.15", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_LARGO_DIV",       "label": "Largo · Dividendo",       "section": "Scoring — Pesos", "type": "number", "default": "0.20", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_LARGO_ROE",       "label": "Largo · ROE",             "section": "Scoring — Pesos", "type": "number", "default": "0.25", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_LARGO_PE",        "label": "Largo · PER",             "section": "Scoring — Pesos", "type": "number", "default": "0.15", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_LARGO_RSI",       "label": "Largo · RSI",             "section": "Scoring — Pesos", "type": "number", "default": "0.00", "hint": "Peso 0.0–1.0.", "restart": False},
    # Scoring weights — medio
    {"key": "SCORE_MEDIO_DRAWDOWN",  "label": "Medio · Drawdown 52w",   "section": "Scoring — Pesos", "type": "number", "default": "0.25", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_MEDIO_MOM3M",     "label": "Medio · Momentum 3m",    "section": "Scoring — Pesos", "type": "number", "default": "0.15", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_MEDIO_VOL",       "label": "Medio · Volatilidad",     "section": "Scoring — Pesos", "type": "number", "default": "0.10", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_MEDIO_DIV",       "label": "Medio · Dividendo",       "section": "Scoring — Pesos", "type": "number", "default": "0.10", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_MEDIO_ROE",       "label": "Medio · ROE",             "section": "Scoring — Pesos", "type": "number", "default": "0.15", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_MEDIO_PE",        "label": "Medio · PER",             "section": "Scoring — Pesos", "type": "number", "default": "0.15", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_MEDIO_RSI",       "label": "Medio · RSI",             "section": "Scoring — Pesos", "type": "number", "default": "0.10", "hint": "Peso 0.0–1.0.", "restart": False},
    # Scoring weights — corto
    {"key": "SCORE_CORTO_DRAWDOWN",  "label": "Corto · Drawdown 52w",   "section": "Scoring — Pesos", "type": "number", "default": "0.25", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_CORTO_MOM3M",     "label": "Corto · Momentum 3m",    "section": "Scoring — Pesos", "type": "number", "default": "0.20", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_CORTO_VOL",       "label": "Corto · Volatilidad",     "section": "Scoring — Pesos", "type": "number", "default": "0.05", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_CORTO_DIV",       "label": "Corto · Dividendo",       "section": "Scoring — Pesos", "type": "number", "default": "0.00", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_CORTO_ROE",       "label": "Corto · ROE",             "section": "Scoring — Pesos", "type": "number", "default": "0.00", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_CORTO_PE",        "label": "Corto · PER",             "section": "Scoring — Pesos", "type": "number", "default": "0.00", "hint": "Peso 0.0–1.0.", "restart": False},
    {"key": "SCORE_CORTO_RSI",       "label": "Corto · RSI",             "section": "Scoring — Pesos", "type": "number", "default": "0.50", "hint": "Peso 0.0–1.0.", "restart": False},
    # Escenario de stress personalizado
    {
        "key": "custom_stress_name",
        "label": "Escenario personalizado — Nombre",
        "hint": "Nombre del escenario de stress personalizado (ej. 'Recesión Europa'). Déjalo vacío para desactivar.",
        "type": "text",
        "restart": False,
        "section": "Stress Testing",
    },
    {
        "key": "custom_stress_pct",
        "label": "Escenario personalizado — Shock (%)",
        "hint": "Porcentaje de variación a aplicar a toda la cartera. Usa valores negativos para caídas (ej. -20) o positivos para subidas (ej. +15).",
        "type": "number",
        "restart": False,
        "section": "Stress Testing",
    },
]


def _missing_required_settings() -> list[str]:
    """Devuelve lista de claves requeridas que no están configuradas (ni en BD ni en env)."""
    from config import ANTHROPIC_API_KEY
    db = get_all_settings()
    missing = []
    for key, env_val in [
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    ]:
        if not db.get(key) and not env_val:
            missing.append(key)
    return missing


def _commission_eur() -> float:
    """Devuelve la comisión por operación configurada (en EUR). Default: 1.0."""
    try:
        return float(effective("COMMISSION_EUR", "", "1.0"))
    except Exception:
        return 1.0


def _get_scoring_weights_from_db() -> dict:
    """Lee los pesos de scoring de la BD. Devuelve dict {horizonte: {factor: val}}."""
    _KEY_MAP = {
        "largo": {
            "drawdown_52w":   "SCORE_LARGO_DRAWDOWN",
            "momentum_3m":    "SCORE_LARGO_MOM3M",
            "volatility":     "SCORE_LARGO_VOL",
            "dividend_yield": "SCORE_LARGO_DIV",
            "roe":            "SCORE_LARGO_ROE",
            "pe_ratio":       "SCORE_LARGO_PE",
            "rsi":            "SCORE_LARGO_RSI",
        },
        "medio": {
            "drawdown_52w":   "SCORE_MEDIO_DRAWDOWN",
            "momentum_3m":    "SCORE_MEDIO_MOM3M",
            "volatility":     "SCORE_MEDIO_VOL",
            "dividend_yield": "SCORE_MEDIO_DIV",
            "roe":            "SCORE_MEDIO_ROE",
            "pe_ratio":       "SCORE_MEDIO_PE",
            "rsi":            "SCORE_MEDIO_RSI",
        },
        "corto": {
            "drawdown_52w":   "SCORE_CORTO_DRAWDOWN",
            "momentum_3m":    "SCORE_CORTO_MOM3M",
            "volatility":     "SCORE_CORTO_VOL",
            "dividend_yield": "SCORE_CORTO_DIV",
            "roe":            "SCORE_CORTO_ROE",
            "pe_ratio":       "SCORE_CORTO_PE",
            "rsi":            "SCORE_CORTO_RSI",
        },
    }
    db = get_all_settings()
    result = {}
    for horizon, factor_map in _KEY_MAP.items():
        result[horizon] = {}
        for factor, key in factor_map.items():
            default = str(_WEIGHTS[horizon][factor])
            try:
                result[horizon][factor] = float(db.get(key, default) or default)
            except (TypeError, ValueError):
                result[horizon][factor] = _WEIGHTS[horizon][factor]
    return result


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
@limiter.limit("5/minute")
async def settings_app_update(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    form = await request.form()

    # Validadores por clave (ISO 27001 A.14.2 — validación de entrada)
    def _valid_setting(key: str, value: str) -> bool:
        if key == "REPORT_HOUR":
            try:
                return 0 <= int(value) <= 23
            except ValueError:
                return False
        if key == "TIMEZONE":
            try:
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                ZoneInfo(value)
                return True
            except Exception:
                return False
        if key == "ANTHROPIC_API_KEY":
            return len(value) <= 500  # evita DoS por valores enormes
        return len(value) <= 2000  # límite genérico para todos los demás campos

    ip = get_remote_address(request)
    valid_keys = {s["key"] for s in _APP_SETTINGS}
    for key in valid_keys:
        value = (form.get(key) or "").strip()
        clear = form.get(f"clear_{key}")
        if clear:
            delete_setting(key)
            # ISO 27001 A.12.4 — auditoría de cambios de configuración
            log_audit_event("setting_deleted", ip_address=ip, details=f"key={key}")
        elif value:
            if _valid_setting(key, value):
                old_val = get_setting(key)
                if old_val != value:
                    set_setting(key, value)
                    # Enmascarar claves sensibles en el log de auditoría
                    masked = "***" if "KEY" in key or "PASSWORD" in key or "SECRET" in key else value[:80]
                    log_audit_event("setting_changed", ip_address=ip, details=f"key={key},new={masked}")
            else:
                logger.warning("Valor inválido para %s ignorado en /settings/app", key)
        # Empty + no clear → leave unchanged
    return RedirectResponse("/settings/app?ok=1", status_code=303)


@app.post("/settings/benchmark-ticker")
@limiter.limit("10/minute")  # ISO 27001 A.12.2
async def settings_benchmark_ticker(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    csrf_token: Optional[str] = Form(default=None),
    ticker:     str = Form(""),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    t = ticker.strip().upper()
    if t and _TICKER_RE.match(t):
        set_setting("benchmark_ticker", t)
    elif not t:
        from database import delete_setting as _del_setting
        _del_setting("benchmark_ticker")
    # Invalidate hist cache so new ticker is fetched fresh
    with _hist_cache_lock:
        _hist_cache.clear()
    return RedirectResponse("/benchmark?saved=1", status_code=303)


@app.get("/logout")
async def logout(request: Request, session: Optional[str] = Cookie(default=None)):
    ip = get_remote_address(request)
    _invalidate_session(session)
    log_audit_event("logout", ip_address=ip)
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
        df_s      = _get_scored_df(df)
        positions = {row[0]: (row[1], row[2]) for row in _get_positions()}

        for d in df_s[df_s["category"] == "portfolio"].to_dict("records"):
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

        for d in df_s[df_s["category"] == "watchlist"] \
                          .sort_values("score", ascending=False).to_dict("records"):
            watchlist.append(d)

    reports = get_recent_reports(n=1)

    tr_cash_row = get_tr_cache("cash_eur")
    tr_cash = None
    if tr_cash_row:
        try:
            tr_cash = float(tr_cash_row[0])
        except (TypeError, ValueError):
            tr_cash = None
    if tr_cash is not None and total_value is not None:
        total_value += tr_cash

    # Antigüedad de datos: días desde el snapshot más reciente en BD
    data_age = None
    try:
        with _db() as _conn:
            _row = _conn.execute("SELECT MAX(date) FROM price_history").fetchone()
            last_update = _row[0] if _row and _row[0] else None
        if last_update:
            _last_date = datetime.date.fromisoformat(last_update)
            data_age = (datetime.date.today() - _last_date).days
    except Exception:
        pass

    has_value_history = len(get_portfolio_value_history(days=365)) >= 2

    # Avisos de seguridad para el administrador (ISO 27001 A.12.4)
    security_warnings = []
    if not COOKIE_SECURE:
        security_warnings.append({
            "level": "warning",
            "msg": "COOKIE_SECURE no está activado. Configura tu reverse proxy con HTTPS y añade COOKIE_SECURE=1 al .env para producción.",
            "link": "/privacy",
        })
    creds_for_warning = _load_credentials()
    days_left = _password_days_remaining(creds_for_warning)
    if days_left is not None and days_left <= 15:
        security_warnings.append({
            "level": "warning" if days_left > 0 else "danger",
            "msg": f"Contraseña {'expirará en ' + str(days_left) + ' días' if days_left > 0 else 'expirada'} (política ISO 27001 A.9.2.1 — 90 días).",
            "link": "/settings/credentials",
        })
    # Advertir si la API key está almacenada en la BD (texto plano) — ISO 27001 A.10
    try:
        if get_setting("ANTHROPIC_API_KEY"):
            security_warnings.append({
                "level": "warning",
                "msg": "La API key de Claude está guardada en la base de datos sin cifrar. Asegúrate de que data/radar.db tiene permisos restrictivos y activa BACKUP_PASSPHRASE.",
                "link": "/settings/app",
            })
    except Exception:
        pass
    # Advertir si VAPID_CONTACT_EMAIL no está configurado — ISO 27001 A.16
    try:
        from push_utils import VAPID_SUBJECT
        if "localhost" in VAPID_SUBJECT:
            security_warnings.append({
                "level": "warning",
                "msg": "VAPID_CONTACT_EMAIL no configurado. Añade tu email en .env para identificar el servicio push (RFC 8292 / ISO 27001 A.16).",
                "link": "/settings/app",
            })
    except Exception:
        pass

    return templates.TemplateResponse("dashboard.html", {
        "request":            request,
        "portfolio":          portfolio,
        "watchlist":          watchlist,
        "total_value":        total_value if total_value else None,
        "last_report":        reports[0] if reports else None,
        "has_data":           df is not None,
        "n_alerts":           len(get_active_alerts()),
        "n_tickers":          len(portfolio) + len(watchlist),
        "tr_cash":            tr_cash,
        "data_age":           data_age,
        "error":              error,
        "has_value_history":  has_value_history,
        "security_warnings":  security_warnings,
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
    log_audit_event("report_triggered", ip_address=get_remote_address(request), details="manual_trigger")
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
    positions = {row[0]: (row[1], row[2]) for row in _get_positions()}
    rows_data = []

    if df is not None:
        for row in df[df["category"] == "portfolio"].to_dict("records"):
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
        if r["target_w"] is not None:
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
        df_s = _get_scored_df(df)
        for d in df_s.to_dict("records"):
            h = d.get("horizon") or "medio"
            if h not in by_horizon:
                h = "medio"
            if d.get("opportunity") in ("ALTA", "MEDIA"):
                by_horizon[h].append(d)

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
    if not _TICKER_RE.match(ticker):
        raise HTTPException(400, "Ticker inválido")

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

_SEARCH_RE = re.compile(r'^[A-Za-z0-9 .\-]{2,50}$')

@app.get("/tickers/search")
@limiter.limit("10/minute")
async def tickers_search(request: Request, q: str = "", session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return JSONResponse([])
    if not _SEARCH_RE.match(q):
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
@limiter.limit("30/minute")
async def tickers_info(request: Request, ticker: str = "", session: Optional[str] = Cookie(default=None)):
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
    for ticker_row, shares, avg_price in _get_positions():
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
        "tr_cash":               _safe_float(tr_cash_row[0]) if tr_cash_row else None,
        "tr_unmatched":          tr_unmatched,
        "active_tab":            tab if tab in ("tickers", "tr") else "tickers",
        "saved":                 saved,
        "error":                 error,
    })


@app.post("/tickers/add")
@limiter.limit("20/minute")
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
    auto_alert:    str = Form(""),
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
    entry: dict = {
        "name":   _sanitize_name(nombre)[:_MAX_NAME_LEN],
        "block":  bloque[:_MAX_BLOCK_LEN],
        "region": region[:_MAX_REGION_LEN],
    }
    if target_weight:
        try:
            entry["target_weight"] = float(target_weight)
        except ValueError:
            pass
    if horizon in ("largo", "medio", "corto"):
        entry["horizon"] = horizon
    tp_float = None
    if target_price:
        try:
            tp_float = float(target_price)
            entry["target_price"] = tp_float
        except ValueError:
            pass
    if notes:
        entry["notes"] = notes.strip()[:_MAX_NOTES_LEN]
    tickers[categoria][t] = entry
    _save_tickers(tickers)
    log_audit_event("ticker_added", ip_address=get_remote_address(request), details=f"ticker={t},categoria={categoria}")

    # Auto-create price alert if requested and target_price is set
    if auto_alert and tp_float is not None and tp_float > 0:
        try:
            df = _read_csv()
            current_price = None
            if df is not None:
                row_df = df[df["ticker"] == t]
                if not row_df.empty:
                    p = row_df.iloc[0].get("price")
                    if p and not _is_nan(p):
                        current_price = float(p)
            direction = "below" if (current_price and tp_float < current_price) else "above"
            add_price_alert(t, tp_float, direction, condition_type="price")
        except Exception:
            logger.exception("Error creando alerta automática para %s", t)

    return RedirectResponse("/tickers", status_code=303)


@app.post("/tickers/update")
@limiter.limit("20/minute")
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
    auto_alert:    str = Form(""),
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
    if nombre:        meta["name"]          = _sanitize_name(nombre)[:_MAX_NAME_LEN]
    if bloque:        meta["block"]         = bloque[:_MAX_BLOCK_LEN]
    if region:        meta["region"]        = region[:_MAX_REGION_LEN]
    if target_weight:
        try:
            meta["target_weight"] = float(target_weight)
        except ValueError:
            pass
    if horizon in ("largo", "medio", "corto"):
        meta["horizon"] = horizon
    tp_float = None
    if target_price:
        try:
            tp_float = float(target_price)
            meta["target_price"] = tp_float
        except ValueError:
            pass
    if notes is not None:
        meta["notes"] = notes.strip()[:_MAX_NOTES_LEN]
    tickers.setdefault(categoria, {})[ticker] = meta
    _save_tickers(tickers)
    log_audit_event("ticker_updated", ip_address=get_remote_address(request), details=f"ticker={ticker.strip().upper()},categoria={categoria}")

    # Auto-create price alert if requested and target_price is set
    if auto_alert and tp_float is not None and tp_float > 0:
        try:
            df = _read_csv()
            current_price = None
            if df is not None:
                row_df = df[df["ticker"] == ticker.strip().upper()]
                if not row_df.empty:
                    p = row_df.iloc[0].get("price")
                    if p and not _is_nan(p):
                        current_price = float(p)
            direction = "below" if (current_price and tp_float < current_price) else "above"
            add_price_alert(ticker.strip().upper(), tp_float, direction, condition_type="price")
        except Exception:
            logger.exception("Error creando alerta automática para %s", ticker)

    return RedirectResponse("/tickers", status_code=303)


@app.post("/tickers/enrich")
@limiter.limit("2/minute")
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
        # Recopilar los que necesitan enriquecimiento
        tasks = []
        for cat in ("portfolio", "watchlist"):
            for ticker, meta in (tickers_data.get(cat) or {}).items():
                if not isinstance(meta, dict):
                    continue
                has_name = meta.get("name") and meta.get("name") != ticker
                if has_name and meta.get("block") and meta.get("region") and meta.get("horizon"):
                    continue
                tasks.append((cat, ticker, dict(meta)))
        if not tasks:
            return
        # Paralelizar los fetches de yfinance (hasta 5 workers)
        from concurrent.futures import ThreadPoolExecutor as _TPE
        def _enrich_one(args):
            cat, ticker, meta = args
            return cat, ticker, _enrich_ticker_meta(ticker, meta)
        with _TPE(max_workers=min(len(tasks), 5)) as pool:
            for cat, ticker, enriched in pool.map(_enrich_one, tasks):
                orig = tickers_data[cat].get(ticker, {})
                if enriched != orig:
                    tickers_data[cat][ticker] = enriched
                    changed = True
    await asyncio.get_running_loop().run_in_executor(_executor, _do_enrich)
    if changed:
        _save_tickers(tickers_data)
    return RedirectResponse("/tickers?enriched=1", status_code=303)


@app.post("/tickers/delete")
@limiter.limit("10/minute")
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
        delete_ticker_record(ticker)
    _save_tickers(tickers)
    log_audit_event("ticker_deleted", ip_address=get_remote_address(request), details=f"ticker={ticker.strip().upper()},categoria={categoria}")
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
        qs = f"?saved={quote(saved, safe='')}"
    elif error:
        qs = f"?error={quote(error, safe='')}"
    return RedirectResponse(f"/tickers{qs}", status_code=302)


@app.post("/posiciones/add")
@limiter.limit("20/minute")
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
    if not _TICKER_RE.match(t):
        return RedirectResponse("/tickers?error=ticker_invalido", status_code=303)
    if 0 < shares < 1_000_000 and 0 < avg_price < 1_000_000:
        upsert_position(t, shares, avg_price)
        _invalidate_positions_cache()
        log_audit_event("position_upserted", ip_address=get_remote_address(request), details=f"ticker={t},shares={shares},avg_price={avg_price}")
        return RedirectResponse(f"/tickers?saved={quote(t, safe='')}", status_code=303)
    return RedirectResponse("/tickers?error=datos_invalidos", status_code=303)


@app.post("/posiciones/delete")
@limiter.limit("10/minute")
async def posiciones_delete(
    request:    Request,
    session: Optional[str] = Cookie(default=None),
    ticker:  str = Form(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    t_del = ticker.strip().upper()
    delete_position(t_del)
    _invalidate_positions_cache()
    log_audit_event("position_deleted", ip_address=get_remote_address(request), details=f"ticker={t_del}")
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
    ticker_filter = ticker.upper() if ticker else None
    ops = get_operations(ticker=ticker_filter, limit=200)
    tickers_yaml = _load_tickers()
    all_tickers = []
    for cat in ("portfolio", "watchlist"):
        for t, meta in (tickers_yaml.get(cat) or {}).items():
            name = meta.get("name", t) if isinstance(meta, dict) else t
            all_tickers.append({"ticker": t, "name": name})
    all_tickers.sort(key=lambda x: x["ticker"])

    # P&L summary — P&L realizado = total ingresado por ventas menos total pagado en compras
    # (simplificación FIFO global: no identifica qué lotes concretos se vendieron)
    total_bought = 0.0
    total_sold = 0.0
    total_commissions = 0.0
    for op in ops:
        op_id, op_ticker, op_date, op_type, op_shares, op_price, op_notes, op_commission = op
        amount = op_shares * op_price
        total_commissions += op_commission or 0.0
        if op_type == "buy":
            total_bought += amount
        else:
            total_sold += amount
    # P&L neto realizado: lo cobrado en ventas menos lo pagado en compras menos comisiones
    pnl_net = total_sold - total_bought - total_commissions
    ops_summary = {
        "total_bought": round(total_bought, 2),
        "total_sold": round(total_sold, 2),
        "pnl_realized": round(pnl_net, 2),
        "total_commissions": round(total_commissions, 2),
    }

    return templates.TemplateResponse("operaciones.html", {
        "request": request,
        "ops": ops,
        "all_tickers": all_tickers,
        "ticker_filter": ticker_filter,
        "saved": saved,
        "count": count_operations(),
        "commission_eur": _commission_eur(),
        "ops_summary": ops_summary,
    })


@app.post("/operaciones/add")
@limiter.limit("20/minute")
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
        op_date = datetime.date.fromisoformat(date)
        if op_date > datetime.date.today():
            return RedirectResponse("/operaciones?error=fecha_futura", status_code=303)
    except ValueError:
        return RedirectResponse("/operaciones", status_code=303)
    add_operation(t, date, op_type, shares, price_eur, notes.strip()[:_MAX_NOTES_LEN], commission_eur=_commission_eur())
    ip = get_remote_address(request)
    log_audit_event("operation_added", ip_address=ip, details=f"ticker={t},type={op_type},shares={shares},date={date}")

    # Auto-sincronizar posición en cartera tras la operación
    try:
        pos = get_portfolio_position(t)
        if op_type == "buy":
            if pos:
                old_shares, old_avg = pos
                new_shares = old_shares + shares
                new_avg = (old_shares * old_avg + shares * price_eur) / new_shares
                upsert_position(t, new_shares, new_avg)
            else:
                upsert_position(t, shares, price_eur)
        elif op_type == "sell":
            if pos:
                old_shares, old_avg = pos
                new_shares = old_shares - shares
                if new_shares <= 0:
                    delete_position(t)
                else:
                    upsert_position(t, new_shares, old_avg)
        _invalidate_positions_cache()
    except Exception:
        logger.exception("Error sincronizando posición tras operación de %s", t)

    return RedirectResponse(f"/operaciones?saved={quote(t, safe='')}", status_code=303)


@app.post("/operaciones/delete")
@limiter.limit("10/minute")
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
    log_audit_event("operation_deleted", ip_address=get_remote_address(request), details=f"op_id={op_id}")
    return RedirectResponse("/operaciones", status_code=303)


@app.get("/operaciones/sugerir-nota")
async def operaciones_sugerir_nota(
    session:   Optional[str] = Cookie(default=None),
    ticker:    str = "",
    op_type:   str = "buy",
    price_eur: float = 0.0,
    date:      str = "",
):
    if not _is_auth(session):
        return JSONResponse({"error": "No autenticado"}, status_code=401)
    t = ticker.strip().upper()
    if not t or not _TICKER_RE.match(t):
        return JSONResponse({"error": "Ticker inválido"}, status_code=400)
    if op_type not in ("buy", "sell") or price_eur <= 0:
        return JSONResponse({"error": "Parámetros inválidos"}, status_code=400)
    if not date:
        date = datetime.date.today().isoformat()
    note = await asyncio.get_running_loop().run_in_executor(
        _executor, suggest_operation_note, t, op_type, price_eur, date
    )
    if not note:
        return JSONResponse({"error": "No se pudo generar la sugerencia"}, status_code=500)
    return JSONResponse({"note": note})


# ── Distribución ───────────────────────────────────────────────────────────────

@app.get("/distribucion", response_class=HTMLResponse)
async def distribucion_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df = _read_csv()
    positions = {row[0]: (row[1], row[2]) for row in _get_positions()}

    by_sector = {}
    by_region = {}
    by_currency: dict = {}
    total = 0.0

    # Region → currency mapping
    _REGION_TO_CURRENCY = {
        "USA":          "USD",
        "Europa":       "EUR",
        "Europe":       "EUR",
        "Reino Unido":  "GBP",
        "UK":           "GBP",
        "Suiza":        "CHF",
        "Japón":        "JPY",
        "Japan":        "JPY",
    }

    if df is not None:
        for row in df.to_dict("records"):
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
            currency = _REGION_TO_CURRENCY.get(region, "OTHER")

            by_sector[sector] = by_sector.get(sector, 0.0) + value
            by_region[region] = by_region.get(region, 0.0) + value
            by_currency[currency] = by_currency.get(currency, 0.0) + value

    def _to_pct_list(d, total):
        items = sorted(d.items(), key=lambda x: -x[1])
        return [{"label": k, "value": v, "pct": round(v / total * 100, 1) if total else 0} for k, v in items]

    currency_dist = []
    for item in _to_pct_list(by_currency, total):
        currency_dist.append({
            "currency": item["label"],
            "pct": item["pct"],
            "value_eur": item["value"],
        })

    return templates.TemplateResponse("distribucion.html", {
        "request": request,
        "by_sector": _to_pct_list(by_sector, total),
        "by_region": _to_pct_list(by_region, total),
        "currency_dist": currency_dist,
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
    positions = {row[0]: (row[1], row[2]) for row in _get_positions()}
    suggestions = []

    if df is not None and importe > 0 and positions:
        rows_data = []
        total_value = 0.0
        for row in df[df["category"] == "portfolio"].to_dict("records"):
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
            r["target_w_eff"] = r["target_w"] if r["target_w"] is not None else (100 / len(rows_data) if rows_data else 0)

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

    # Modo inverso: cuánto hay que invertir en cada ticker para alcanzar su peso objetivo
    inverse_allocation = []
    if df is not None and positions:
        inv_rows = []
        total_val = 0.0
        for row in df[df["category"] == "portfolio"].to_dict("records"):
            ticker = row["ticker"]
            if ticker not in positions:
                continue
            price = row.get("price")
            if not price or _is_nan(price):
                continue
            shares, _ = positions[ticker]
            value = shares * float(price)
            total_val += value
            try:
                _tw_raw = row.get("target_weight")
                tw = float(_tw_raw) if _tw_raw is not None and not _is_nan(_tw_raw) else None
            except (TypeError, ValueError):
                tw = None
            inv_rows.append({"ticker": ticker, "name": row["name"],
                             "price": float(price), "value": value, "target_w": tw})
        if total_val > 0 and inv_rows:
            n = len(inv_rows)
            for r in inv_rows:
                r["current_w"] = r["value"] / total_val * 100
                r["target_w_eff"] = r["target_w"] if r["target_w"] is not None else (100 / n)
                target_value = total_val * r["target_w_eff"] / 100
                deficit = target_value - r["value"]
                r["needed_eur"] = round(max(0, deficit), 2)
                r["needed_shares"] = round(r["needed_eur"] / r["price"], 4) if r["price"] > 0 and r["needed_eur"] > 0 else 0
            inverse_allocation = sorted(inv_rows, key=lambda x: -x["needed_eur"])

    return templates.TemplateResponse("simulador.html", {
        "request": request,
        "importe": importe,
        "suggestions": suggestions,
        "has_data": df is not None,
        "has_positions": bool(positions),
        "inverse_allocation": inverse_allocation,
    })


# ── Benchmark ─────────────────────────────────────────────────────────────────

@app.get("/benchmark", response_class=HTMLResponse)
async def benchmark_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    positions = _get_positions()
    has_positions = bool(positions)
    value_history = get_portfolio_value_history(days=365)
    has_history = len(value_history) >= 5
    custom_ticker = (get_setting("benchmark_ticker") or "").strip().upper() or None

    return templates.TemplateResponse("benchmark.html", {
        "request": request,
        "has_positions": has_positions,
        "has_history": has_history,
        "value_history": value_history,
        "custom_ticker": custom_ticker,
        "saved": request.query_params.get("saved"),
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
        df_s = _get_scored_df(df)
        for d in df_s.to_dict("records"):
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
    scores: dict = {}
    if df is not None:
        for r in df.sort_values("ticker").to_dict("records"):
            tickers_disponibles.append({"ticker": r["ticker"], "name": r.get("name", r["ticker"])})
            sc = r.get("score")
            if sc is not None and not _is_nan(sc):
                scores[r["ticker"]] = round(float(sc), 2)

    # Enrich stoploss_pct alerts with absolute stop price from avg_price
    positions_map = {row[0]: (row[1], row[2]) for row in _get_positions()}
    raw_alerts = get_active_alerts()
    alerts_enriched = []
    for row in raw_alerts:
        # row: id, ticker, target_price, direction, created, ctype, cvalue, expires_at
        row = list(row)
        ctype = row[5] if len(row) > 5 else "price"
        ticker_a = row[1]
        stop_abs = None
        if ctype == "stoploss_pct":
            target_pct = row[2]  # % loss vs cost
            pos = positions_map.get(ticker_a)
            if pos:
                avg_p = pos[1]
                if avg_p and avg_p > 0 and target_pct is not None:
                    stop_abs = round(avg_p * (1 - float(target_pct) / 100), 2)
        row_dict = {
            "id": row[0], "ticker": row[1], "target": row[2], "direction": row[3],
            "created": row[4], "ctype": ctype,
            "cvalue": row[6] if len(row) > 6 else None,
            "expires_at": row[7] if len(row) > 7 else None,
            "stop_abs": stop_abs,
        }
        alerts_enriched.append(row_dict)

    return templates.TemplateResponse("alertas.html", {
        "request":             request,
        "alerts":              alerts_enriched,
        "alert_history":       get_alert_history(limit=30),
        "tickers_disponibles": tickers_disponibles,
        "scores":              scores,
        "alerts_raw":          raw_alerts,
    })


@app.post("/alertas/add")
@limiter.limit("20/minute")
async def alertas_add(
    request:        Request,
    session:        Optional[str] = Cookie(default=None),
    ticker:         str   = Form(...),
    condition_type: str   = Form("price"),
    target_price:   float = Form(...),
    expires_at:     Optional[str] = Form(default=None),
    csrf_token:     Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    t = ticker.strip().upper()

    # Validate and sanitize expires_at (ISO 27001 A.14.2 — validación de fechas)
    _expires_at = None
    if expires_at and expires_at.strip():
        try:
            exp_date = datetime.date.fromisoformat(expires_at.strip())
            today = datetime.date.today()
            if exp_date < today:
                return RedirectResponse("/alertas?error=fecha_pasada", status_code=303)
            if (exp_date - today).days > 3650:  # máx. 10 años
                return RedirectResponse("/alertas?error=fecha_lejana", status_code=303)
            _expires_at = expires_at.strip()
        except ValueError:
            return RedirectResponse("/alertas?error=fecha_invalida", status_code=303)

    if condition_type == "price_pct":
        # Alerta por % desde precio actual
        pct = target_price  # puede ser negativo (caída) o positivo (subida)
        if not (-100 <= pct <= 500) or pct == 0:
            return RedirectResponse("/alertas?error=rango", status_code=303)
        df = _read_csv()
        current_price = None
        if df is not None:
            row = df[df["ticker"] == t]
            if not row.empty:
                p = row.iloc[0].get("price")
                if p and not _is_nan(p):
                    current_price = float(p)
        if not current_price:
            return RedirectResponse("/alertas?error=no_price", status_code=303)
        direction = "below" if pct < 0 else "above"
        add_price_alert(t, pct, direction, condition_type="price_pct",
                        condition_value=current_price, expires_at=_expires_at)
        log_audit_event("alert_created", ip_address=get_remote_address(request), details=f"ticker={t},type=price_pct,value={pct}")
        return RedirectResponse("/alertas", status_code=303)
    elif condition_type == "stoploss_pct":
        # Stop-loss dinámico: % de pérdida desde precio de compra
        pct = abs(target_price)
        if not (0 < pct <= 100):
            return RedirectResponse("/alertas?error=rango", status_code=303)
        add_price_alert(t, pct, "below", condition_type="stoploss_pct",
                        condition_value=pct, expires_at=_expires_at)
        log_audit_event("alert_created", ip_address=get_remote_address(request), details=f"ticker={t},type=stoploss_pct,value={pct}")
        return RedirectResponse("/alertas", status_code=303)
    elif condition_type == "drawdown":
        # Los drawdowns son porcentajes negativos (-100 a 0)
        # Normalizar: si el usuario introduce positivo, convertir a negativo
        target_price = -abs(target_price)
        if not (-100 <= target_price < 0):
            return RedirectResponse("/alertas?error=rango", status_code=303)
        add_price_alert(t, target_price, "below", condition_type=condition_type,
                        condition_value=target_price, expires_at=_expires_at)
        log_audit_event("alert_created", ip_address=get_remote_address(request), details=f"ticker={t},type=drawdown,value={target_price}")
        return RedirectResponse("/alertas", status_code=303)
    elif condition_type == "score":
        # El score debe estar entre 0 y 100
        if not (0 <= target_price <= 100):
            return RedirectResponse("/alertas?error=rango", status_code=303)
        add_price_alert(t, target_price, "above", condition_type=condition_type,
                        condition_value=target_price, expires_at=_expires_at)
        log_audit_event("alert_created", ip_address=get_remote_address(request), details=f"ticker={t},type=score,value={target_price}")
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
    add_price_alert(t, target_price, direction, condition_type="price", expires_at=_expires_at)
    log_audit_event("alert_created", ip_address=get_remote_address(request), details=f"ticker={t},type=price,value={target_price}")
    return RedirectResponse("/alertas", status_code=303)


@app.post("/alertas/delete")
@limiter.limit("10/minute")
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
    log_audit_event("alert_deleted", ip_address=get_remote_address(request), details=f"alert_id={alert_id}")
    return RedirectResponse("/alertas", status_code=303)


@app.post("/alertas/reactivar")
@limiter.limit("10/minute")
async def alertas_reactivar(
    request:         Request,
    session:         Optional[str] = Cookie(default=None),
    ticker:          str   = Form(...),
    target_price:    float = Form(...),
    direction:       str   = Form(...),
    condition_type:  str   = Form("price"),
    condition_value: str   = Form(""),
    csrf_token:      Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    t = ticker.strip().upper()
    if not _TICKER_RE.match(t):
        return RedirectResponse("/alertas", status_code=303)
    if direction not in ("above", "below"):
        return RedirectResponse("/alertas", status_code=303)
    _cvalue = None
    if condition_value.strip():
        try:
            _cvalue = float(condition_value.strip())
        except (TypeError, ValueError):
            _cvalue = None
    add_price_alert(t, target_price, direction, condition_type=condition_type, condition_value=_cvalue)
    return RedirectResponse("/alertas", status_code=303)


# ── Trade Republic ────────────────────────────────────────────────────────────

@app.post("/tr/setup/start")
@limiter.limit("5/minute")  # ISO 27001 A.12.2 — previene abuso del setup Trade Republic
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
@limiter.limit("5/minute")  # ISO 27001 A.12.2
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
@limiter.limit("3/minute")  # ISO 27001 A.12.2 — sincronización TR costosa
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
        delete_ticker_record(bad_ticker)
        delete_tr_isin(isin)
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

    if synced > 0:
        _invalidate_positions_cache()

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
        logger.error("Error obteniendo gráfico TR: %s", e)
        raise HTTPException(503, "Error al obtener datos de Trade Republic")

    if fig is None:
        raise HTTPException(404, "Sin datos")
    return _fig_to_response(fig)


# ── Reportes ──────────────────────────────────────────────────────────────────

# ── Dividendos ────────────────────────────────────────────────────────────────

@app.get("/dividendos", response_class=HTMLResponse)
async def dividendos_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    positions = {row[0]: (row[1], row[2]) for row in _get_positions()}

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

                try:
                    divs = stock.dividends
                except Exception:
                    logger.warning("Error calculando dividendos de %s", ticker)
                    continue
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

                # Próxima fecha ex-dividendo y fecha de pago
                next_exdate = None
                ex_ts = info.get("exDividendDate")
                if ex_ts:
                    try:
                        next_exdate = datetime.date.fromtimestamp(ex_ts).isoformat()
                    except (OSError, OverflowError, ValueError):
                        pass

                dividend_date = None
                div_ts = info.get("dividendDate")
                if div_ts:
                    try:
                        dividend_date = datetime.date.fromtimestamp(div_ts).isoformat()
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

                # Calendario mensual: qué mes paga y cuánto (para próx. 24 meses)
                monthly_calendar = {}
                for m, avg_div in monthly_est.items():
                    amount_eur = to_eur(avg_div * shares, currency)
                    if amount_eur and not math.isnan(amount_eur):
                        monthly_calendar[m] = round(amount_eur, 2)

                results.append({
                    "ticker":           ticker,
                    "name":             info.get("longName") or info.get("shortName") or ticker,
                    "shares":           shares,
                    "q1_eur":           quarterly_eur[1],
                    "q2_eur":           quarterly_eur[2],
                    "q3_eur":           quarterly_eur[3],
                    "q4_eur":           quarterly_eur[4],
                    "annual_eur":       round(annual_eur, 2),
                    "yield_pct":        yield_pct,
                    "frequency":        frequency,
                    "next_exdate":      next_exdate,
                    "dividend_date":    dividend_date,
                    "monthly_calendar": monthly_calendar,
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

    # Build calendar_data: mes 1-12 = año actual, 13-24 = próximo año
    today     = datetime.date.today()
    cur_year  = today.year
    calendar_data = {}  # key=1..24 → list of {ticker, amount_eur}
    for r in rows:
        monthly_calendar = r.get("monthly_calendar", {})
        for month, amount_eur in monthly_calendar.items():
            if amount_eur > 0:
                # Current year month index 1..12
                calendar_data.setdefault(month, []).append(
                    {"ticker": r["ticker"], "amount_eur": amount_eur}
                )
                # Next year same month: index 13..24
                calendar_data.setdefault(month + 12, []).append(
                    {"ticker": r["ticker"], "amount_eur": amount_eur}
                )

    # Monthly totals
    calendar_totals = {m: round(sum(e["amount_eur"] for e in entries), 2)
                       for m, entries in calendar_data.items()}

    return templates.TemplateResponse("dividendos.html", {
        "request":        request,
        "rows":           rows,
        "totals":         totals,
        "year":           cur_year,
        "calendar_data":  calendar_data,
        "calendar_totals": calendar_totals,
    })


# ── Consenso de analistas ─────────────────────────────────────────────────────

@app.get("/analistas", response_class=HTMLResponse)
async def analistas_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    def _fetch_all():
        from concurrent.futures import ThreadPoolExecutor, as_completed
        tickers_data = _load_tickers()
        all_tickers = []
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
            rec_mean   = info.get("recommendationMean")
            rec_key    = info.get("recommendationKey", "")
            n_analysts = info.get("numberOfAnalystOpinions") or 0
            tgt_mean   = info.get("targetMeanPrice")
            tgt_high   = info.get("targetHighPrice")
            tgt_low    = info.get("targetLowPrice")
            current    = info.get("currentPrice") or info.get("regularMarketPrice")
            currency   = info.get("currency", "USD")
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
                "ticker":   t, "name": name, "cat": cat,
                "rec_mean": round(rec_mean, 1) if rec_mean is not None else None,
                "rec_key":  rec_key,
                "n":        int(n_analysts),
                "tgt_mean": round(tgt_mean, 2) if tgt_mean is not None else None,
                "tgt_high": round(tgt_high, 2) if tgt_high is not None else None,
                "tgt_low":  round(tgt_low, 2) if tgt_low is not None else None,
                "current":  round(current, 2) if current is not None else None,
                "upside":   round(upside, 1) if upside is not None else None,
            }

        rows = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_fetch_one, item): item for item in all_tickers}
            for fut in as_completed(futs, timeout=90):
                try:
                    r = fut.result()
                    if r["n"] > 0 or r["rec_key"]:
                        rows.append(r)
                except Exception:
                    pass
        rows.sort(key=lambda x: -(x["upside"] if x["upside"] is not None else -999))
        return rows

    rows = await asyncio.get_running_loop().run_in_executor(_executor, _fetch_all)
    return templates.TemplateResponse("analistas.html", {
        "request": request, "rows": rows,
    })


# ── Earnings próximos ─────────────────────────────────────────────────────────

@app.get("/earnings", response_class=HTMLResponse)
async def earnings_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    def _fetch_all():
        import datetime as _dt
        from concurrent.futures import ThreadPoolExecutor, as_completed
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
                    elif cal_df is not None:
                        logger.debug("calendar unexpected type for %s: %s keys=%s", t, type(cal_df).__name__, list(cal_df.keys()) if hasattr(cal_df, "keys") else "n/a")
                except Exception:
                    pass
                earnings_date = None
                if cal.get("Earnings Date"):
                    ed = cal["Earnings Date"]
                    earnings_date = ed[0] if isinstance(ed, list) else ed
                elif info.get("earningsDate"):
                    ts = info["earningsDate"]
                    if isinstance(ts, (int, float)):
                        earnings_date = _dt.date.fromtimestamp(ts)
                eps_est  = cal.get("Earnings Average") or cal.get("EPS Estimate") or cal.get("epsAverage")
                rev_est  = cal.get("Revenue Average") or cal.get("Revenue Estimate") or cal.get("revenueAverage")
                eps_avg  = float(eps_est) if eps_est is not None else None
                rev_avg  = float(rev_est) / 1e9 if rev_est is not None else None
                days_until = None
                if earnings_date:
                    today = _dt.date.today()
                    if hasattr(earnings_date, "date"):
                        earnings_date = earnings_date.date()
                    days_until = (earnings_date - today).days
                return {
                    "ticker": t, "name": name, "cat": cat,
                    "earnings_date": str(earnings_date) if earnings_date else None,
                    "days_until": days_until,
                    "eps_est":   round(eps_avg, 2) if eps_avg is not None else None,
                    "rev_est_b": round(rev_avg, 2) if rev_avg is not None else None,
                }
            except Exception:
                return {"ticker": t, "name": name, "cat": cat,
                        "earnings_date": None, "days_until": None,
                        "eps_est": None, "rev_est_b": None}

        rows = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_fetch_one, item): item for item in all_tickers}
            for fut in as_completed(futs, timeout=90):
                try:
                    r = fut.result()
                    if r["earnings_date"]:
                        rows.append(r)
                except Exception:
                    pass

        today = _dt.date.today()
        upcoming = sorted([r for r in rows if r["days_until"] is not None and r["days_until"] >= 0],
                          key=lambda x: x["days_until"])
        past     = sorted([r for r in rows if r["days_until"] is not None and r["days_until"] < 0],
                          key=lambda x: -x["days_until"])
        return upcoming, past

    upcoming, past = await asyncio.get_running_loop().run_in_executor(_executor, _fetch_all)
    return templates.TemplateResponse("earnings.html", {
        "request": request, "upcoming": upcoming, "past": past,
    })


# ── Correlación entre activos ──────────────────────────────────────────────────

@app.get("/correlacion", response_class=HTMLResponse)
async def correlacion_page(
    request:          Request,
    include_watchlist: bool = False,
    session:          Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    positions = _get_positions()
    tickers   = [r[0] for r in positions]
    if include_watchlist:
        tickers_data = _load_tickers()
        watchlist_tickers = list((tickers_data.get("watchlist") or {}).keys())
        tickers = list(dict.fromkeys(tickers + watchlist_tickers))  # deduplicate, preserve order
    return templates.TemplateResponse("correlacion.html", {
        "request": request, "tickers": tickers, "n": len(tickers),
        "include_watchlist": include_watchlist,
    })


@app.get("/chart/correlacion")
@limiter.limit("10/minute")
async def chart_correlacion(
    request: Request,
    include_watchlist: bool = False,
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        raise HTTPException(status_code=401)

    positions = _get_positions()
    tickers   = [r[0] for r in positions]
    if include_watchlist:
        tickers_data = _load_tickers()
        watchlist_tickers = list((tickers_data.get("watchlist") or {}).keys())
        tickers = list(dict.fromkeys(tickers + watchlist_tickers))
    if len(tickers) < 2:
        raise HTTPException(status_code=400, detail="Se necesitan al menos 2 posiciones")

    def _compute_and_render():
        frames = {}
        for t in tickers:
            try:
                hist = _get_ticker_hist(t, period="1y")["Close"]
                if not hist.empty:
                    # Normalize to plain date so all tickers align regardless of exchange timezone
                    hist.index = pd.to_datetime(hist.index.date)
                    frames[t] = hist
            except Exception:
                pass
        if len(frames) < 2:
            return None
        df   = pd.DataFrame(frames).dropna(how="all")
        rets = df.pct_change().dropna()
        corr = rets.corr()

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

    def _compute():
        import warnings
        warnings.filterwarnings("ignore")
        df        = _read_csv()
        positions = {r[0]: (r[1], r[2]) for r in _get_positions()}
        if df is None:
            return {"_error": "csv"}
        if not positions:
            return {"_error": "positions"}
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
            return {"_error": f"no_prices:tickers={tickers[:5]}"}

        total = sum(values.values())
        weights = {t: v / total for t, v in values.items()}

        # Fetch 1y daily prices
        price_data = {}
        for t in values:
            try:
                hist = _get_ticker_hist(t, period="1y")["Close"]
                if len(hist) > 30:
                    hist.index = pd.to_datetime(hist.index.date)
                    price_data[t] = hist
            except Exception as e:
                logger.warning("riesgo history %s: %s", t, e)

        macro_tickers = {"SPY": "S&P 500", "^VIX": "VIX", "^TNX": "Bono 10Y EE.UU."}
        for mt in macro_tickers:
            try:
                hist = _get_ticker_hist(mt, period="1y")["Close"]
                if not hist.empty:
                    hist.index = pd.to_datetime(hist.index.date)
                    price_data[mt] = hist
            except Exception:
                pass

        if len(price_data) < 2:
            return {"_error": f"yfinance:ok={list(price_data.keys())}"}

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

        # Contribución a la volatilidad de cartera por ticker (MCTR)
        # contrib_i = w_i × cov(r_i, r_p) / var_p  → suma = 100%
        port_var = vol_daily ** 2
        vol_contrib = []
        for t, w in weights.items():
            if t not in df_returns.columns:
                continue
            try:
                cov_ip = float(df_returns[t].cov(port_rets))
                contrib_pct = (w * cov_ip / port_var * 100) if port_var > 0 else 0
                vol_i = float(df_returns[t].std()) * np.sqrt(252) * 100
                vol_contrib.append({
                    "ticker":      t,
                    "weight_pct":  round(w * 100, 1),
                    "vol_annual":  round(vol_i, 1),
                    "contrib_pct": round(contrib_pct, 1),
                })
            except Exception:
                pass
        vol_contrib.sort(key=lambda x: x["contrib_pct"], reverse=True)

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
            "vol_contrib": vol_contrib,
        }

    var_data = await asyncio.get_running_loop().run_in_executor(_executor, _compute)

    # Si _compute devolvió un dict de error diagnóstico, loguearlo y tratarlo como sin datos
    error_msg = None
    if isinstance(var_data, dict) and "_error" in var_data:
        error_msg = var_data["_error"]
        logger.warning("riesgo sin datos: %s", error_msg)
        var_data = None

    return templates.TemplateResponse("riesgo.html", {
        "request":   request,
        "has_data":  var_data is not None,
        "var_data":  var_data,
        "total":     var_data.get("total", 0) if var_data else 0,
        "error_msg": error_msg,
    })


@app.get("/chart/riesgo/returns")
@limiter.limit("10/minute")
async def chart_riesgo_returns(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(status_code=401)

    def _fetch():
        df        = _read_csv()
        positions = {r[0]: (r[1], r[2]) for r in _get_positions()}
        if not positions or df is None:
            return None
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
                h = _get_ticker_hist(t, period="1y")["Close"]
                if len(h) > 30:
                    h.index = pd.to_datetime(h.index.date)
                    frames[t] = h
            except Exception:
                pass
        if not frames:
            return None
        df_r = pd.DataFrame(frames).dropna(how="all").pct_change().dropna()
        port = pd.Series(0.0, index=df_r.index)
        for t, w in weights.items():
            if t in df_r.columns:
                port += df_r[t] * w
        return port.dropna()

    def _fetch_and_render():
        port_rets = _fetch()
        if port_rets is None:
            return None
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

# ── Optimización de cartera (Markowitz + paridad de riesgo) ───────────────────

def _compute_optimization(df_portfolio, positions_map: dict) -> Optional[dict]:
    """
    Calcula tres carteras óptimas usando scipy + datos del reporte:

    Retornos esperados (multi-factor):
      40% retorno histórico 1 año (yfinance)
      20% score del radar (datos reporte)
      15% upside precio objetivo analistas (datos reporte)
      15% momentum 3m/6m (datos reporte)
      10% calidad fundamental: ROE, PER (datos reporte)

    Carteras calculadas:
      - Mínima varianza
      - Máximo Sharpe (score-aware)
      - Paridad de riesgo

    Devuelve dict con pesos, métricas y puntos de frontera eficiente.
    """
    import warnings
    warnings.filterwarnings("ignore")
    try:
        from scipy.optimize import minimize
    except ImportError:
        logger.error("scipy no está instalado. Instala con: pip install scipy")
        return None

    # ─── 1. Tickers con precio en el reporte ──────────────────────────────────
    valid = []
    for t in positions_map:
        row = df_portfolio[df_portfolio["ticker"] == t]
        if row.empty:
            continue
        price = _safe_float(row.iloc[0].get("price"))
        if price and price > 0:
            valid.append(t)

    if len(valid) < 2:
        logger.warning("_compute_optimization: menos de 2 tickers válidos con precio (%d)", len(valid))
        return None

    # ─── 2. Histórico de precios 1 año ────────────────────────────────────────
    price_data = {}
    for t in valid:
        try:
            hist = _get_ticker_hist(t, period="1y")["Close"]
            if len(hist) > 30:
                hist.index = pd.to_datetime(hist.index.date)
                price_data[t] = hist
        except Exception:
            logger.warning("_compute_optimization: error obteniendo histórico de %s", t)

    valid = [t for t in valid if t in price_data]
    if len(valid) < 2:
        logger.warning("_compute_optimization: menos de 2 tickers con histórico suficiente (%d)", len(valid))
        return None

    df_prices = pd.DataFrame({t: price_data[t] for t in valid}).dropna()
    if len(df_prices) < 30:
        logger.warning("_compute_optimization: df_prices tiene solo %d filas tras dropna", len(df_prices))
        return None

    rets = df_prices.pct_change().dropna()
    cov_annual = rets.cov().values * 252   # matriz de covarianzas anualizada
    n = len(valid)

    # ─── 3. Retornos esperados multi-factor ───────────────────────────────────
    mu = np.zeros(n)
    mu_components = []   # para mostrar en UI

    for i, t in enumerate(valid):
        row = df_portfolio[df_portfolio["ticker"] == t].iloc[0]

        # Componente 1: retorno histórico anualizado (40%)
        hist_ret = float(rets[t].mean()) * 252

        # Componente 2: score del radar → ±8% (20%)
        score = _safe_float(row.get("score"))
        score_adj = ((score - 12) / 20) * 0.08 if score is not None else 0.0

        # Componente 3: upside precio objetivo analistas (15%)
        analyst_target = _safe_float(row.get("analyst_target"))
        current_price  = _safe_float(row.get("price"))
        analyst_n      = _safe_float(row.get("analyst_n"), 0)
        if analyst_target and current_price and current_price > 0:
            raw_upside = analyst_target / current_price - 1
            # ponderar por número de analistas (más analistas = más confianza)
            confidence = min(1.0, (analyst_n or 1) / 15.0)
            analyst_mu = raw_upside * confidence
        else:
            analyst_mu = 0.0

        # Componente 4: momentum 3m / 6m (15%)
        mom3 = _safe_float(row.get("momentum_3m"), 0.0) or 0.0
        mom6 = _safe_float(row.get("momentum_6m"), 0.0) or 0.0
        momentum_adj = (mom3 * 0.6 + mom6 * 0.4) / 100.0   # % → fracción

        # Componente 5: calidad fundamental — ROE y PER (10%)
        roe = _safe_float(row.get("roe"))        # viene en % (ej: 18.5)
        per = _safe_float(row.get("pe_ratio"))   # ratio (ej: 15.3)
        fund_adj = 0.0
        if roe is not None and roe > 0:
            fund_adj += min(0.04, (roe - 10) / 100)   # ROE>10% → hasta +4%
        if per is not None and 0 < per < 40:
            fund_adj -= (per - 15) / 500               # PER alto → penalización

        # Blend ponderado (suma de pesos = 1.0)
        horizon = str(row.get("horizon") or "medio")
        if horizon == "corto":
            w = (0.35, 0.20, 0.10, 0.25, 0.10)
        elif horizon == "largo":
            w = (0.40, 0.20, 0.20, 0.05, 0.15)
        else:   # medio (default)
            w = (0.40, 0.20, 0.15, 0.15, 0.10)

        mu[i] = (w[0]*hist_ret + w[1]*score_adj + w[2]*analyst_mu
                 + w[3]*momentum_adj + w[4]*fund_adj)

        mu_components.append({
            "ticker":    t,
            "mu_pct":    round(mu[i] * 100, 2),
            "hist_ret":  round(hist_ret * 100, 2),
            "score_adj": round(score_adj * 100, 2),
            "analyst":   round(analyst_mu * 100, 2),
            "momentum":  round(momentum_adj * 100, 2),
            "fund_adj":  round(fund_adj * 100, 2),
        })

    # ─── 4. Pesos actuales ────────────────────────────────────────────────────
    values = {}
    for t in valid:
        shares, _ = positions_map[t]
        price = float(df_portfolio[df_portfolio["ticker"] == t].iloc[0]["price"])
        values[t] = shares * price
    total_val = sum(values.values())
    current_w = np.array([values[t] / total_val for t in valid])

    # ─── 5. Helpers de métricas ───────────────────────────────────────────────
    RISK_FREE = 0.03  # 3% tasa libre de riesgo (ref. Bund 10Y aprox)

    def portfolio_stats(w):
        w = np.clip(w, 0, 1)
        w = w / w.sum()
        port_ret = float(w @ mu)
        port_vol = float(np.sqrt(max(w @ cov_annual @ w, 0)))
        sharpe   = (port_ret - RISK_FREE) / port_vol if port_vol > 1e-8 else 0.0
        return port_ret, port_vol, sharpe

    def _stats_dict(w, name=""):
        w = np.clip(w, 0, 1)
        w = w / w.sum()
        r, v, s = portfolio_stats(w)
        return {
            "name":    name,
            "ret_pct": round(r * 100, 2),
            "vol_pct": round(v * 100, 2),
            "sharpe":  round(s, 3),
            "weights": {t: round(float(wi) * 100, 1) for t, wi in zip(valid, w)},
        }

    # ─── 6. Constraints y bounds comunes ─────────────────────────────────────
    w0 = np.ones(n) / n
    # Max 40% por posición o 3× equal-weight lo que sea menor
    max_w = min(0.40, max(3.0 / n, 0.10))
    bounds = [(0.01, max_w)] * n
    cons_sum = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]

    # ─── 7. Mínima varianza ───────────────────────────────────────────────────
    res_mv = minimize(
        lambda w: float(w @ cov_annual @ w),
        w0, method="SLSQP", bounds=bounds, constraints=cons_sum,
        options={"ftol": 1e-10, "maxiter": 1000},
    )
    mv_w = res_mv.x if res_mv.success else w0.copy()

    # ─── 8. Máximo Sharpe ────────────────────────────────────────────────────
    def neg_sharpe(w):
        r = float(w @ mu)
        v = float(np.sqrt(max(w @ cov_annual @ w, 1e-16)))
        return -(r - RISK_FREE) / v

    res_ms = minimize(
        neg_sharpe, w0, method="SLSQP", bounds=bounds, constraints=cons_sum,
        options={"ftol": 1e-10, "maxiter": 1000},
    )
    ms_w = res_ms.x if res_ms.success else w0.copy()

    # ─── 9. Paridad de riesgo ─────────────────────────────────────────────────
    def risk_parity_obj(w):
        pv = float(w @ cov_annual @ w)
        if pv <= 0:
            return 1e10
        rc = w * (cov_annual @ w) / pv   # contribuciones de riesgo (suma=1)
        target = np.ones(n) / n
        return float(np.sum((rc - target) ** 2))

    res_rp = minimize(
        risk_parity_obj, w0, method="SLSQP", bounds=bounds, constraints=cons_sum,
        options={"ftol": 1e-12, "maxiter": 2000},
    )
    rp_w = res_rp.x if res_rp.success else w0.copy()

    # ─── 10. Frontera eficiente (40 puntos) ──────────────────────────────────
    ret_min = float(mu.min())
    ret_max = float(mu.max())
    frontier = []
    for target in np.linspace(ret_min, ret_max, 40):
        cons_ef = cons_sum + [{"type": "eq", "fun": lambda w, t=target: float(w @ mu) - t}]
        res_ef = minimize(
            lambda w: float(w @ cov_annual @ w),
            w0, method="SLSQP", bounds=bounds, constraints=cons_ef,
            options={"ftol": 1e-9, "maxiter": 500},
        )
        if res_ef.success:
            vol_f = float(np.sqrt(max(res_ef.fun, 0)))
            frontier.append({"ret": round(target * 100, 2), "vol": round(vol_f * 100, 2)})

    # ─── 11. Nombres de tickers ───────────────────────────────────────────────
    names = {}
    for t in valid:
        row = df_portfolio[df_portfolio["ticker"] == t].iloc[0]
        names[t] = str(row.get("name", t))

    return {
        "tickers":      valid,
        "names":        names,
        "total":        round(total_val, 2),
        "current":      _stats_dict(current_w, "Actual"),
        "min_var":      _stats_dict(mv_w,      "Mínima varianza"),
        "max_sharpe":   _stats_dict(ms_w,      "Máximo Sharpe"),
        "risk_parity":  _stats_dict(rp_w,      "Paridad de riesgo"),
        "mu_components": mu_components,
        "frontier":     frontier,
        "risk_free_pct": RISK_FREE * 100,
        "conv": {
            "min_var":     res_mv.success,
            "max_sharpe":  res_ms.success,
            "risk_parity": res_rp.success,
        },
    }


def _get_opt_cached(df_portfolio, positions_map: dict) -> Optional[dict]:
    """Devuelve la optimización cacheada (TTL 5 min) o la recalcula."""
    with _opt_cache_lock:
        if _opt_cache["data"] is not None and _time.monotonic() - _opt_cache["ts"] < _OPT_CACHE_TTL:
            return _opt_cache["data"]
        result = _compute_optimization(df_portfolio, positions_map)
        _opt_cache["data"] = result
        _opt_cache["ts"]   = _time.monotonic()
        return result


@app.get("/optimizacion", response_class=HTMLResponse)
async def optimizacion_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df = _read_csv()
    positions = {r[0]: (r[1], r[2]) for r in _get_positions()}

    if df is None or not positions:
        return templates.TemplateResponse("optimizacion.html", {
            "request": request, "has_data": False, "opt": None,
        })

    df_port = df[df["category"] == "portfolio"]

    opt = await asyncio.get_running_loop().run_in_executor(
        _executor, _get_opt_cached, df_port, positions,
    )
    return templates.TemplateResponse("optimizacion.html", {
        "request":  request,
        "has_data": opt is not None,
        "opt":      opt,
    })


@app.get("/chart/frontera-eficiente")
@limiter.limit("10/minute")
async def chart_frontera_eficiente(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(status_code=401)

    df = _read_csv()
    positions = {r[0]: (r[1], r[2]) for r in _get_positions()}
    if df is None or not positions:
        raise HTTPException(status_code=400, detail="Sin datos")

    df_port = df[df["category"] == "portfolio"]

    def _make():
        opt = _get_opt_cached(df_port, positions)
        if not opt:
            logger.warning("chart/frontera-eficiente: _get_opt_cached devolvió None")
            return None

        with _chart_lock:
            fig, ax = plt.subplots(figsize=(9, 5.5))
            fig.patch.set_facecolor(_C_BG)
            ax.set_facecolor(_C_BG)

            # Frontera eficiente
            if opt["frontier"]:
                fvols = [p["vol"] for p in opt["frontier"]]
                frets = [p["ret"] for p in opt["frontier"]]
                ax.plot(fvols, frets, color=_C_BLUE, linewidth=2.5,
                        label="Frontera eficiente", zorder=2, alpha=0.9)

            # Capital Market Line desde tasa libre de riesgo al punto Máx. Sharpe
            ms = opt["max_sharpe"]
            rf = opt["risk_free_pct"]
            if ms["vol_pct"] > 0:
                slope  = (ms["ret_pct"] - rf) / ms["vol_pct"]
                cml_x  = [0, ms["vol_pct"] * 1.6]
                cml_y  = [rf, rf + slope * ms["vol_pct"] * 1.6]
                ax.plot(cml_x, cml_y, color="#6e7681", linewidth=1,
                        linestyle="--", label="Capital Market Line", zorder=1)

            # Las 4 carteras
            _portf_spec = [
                ("Actual",           opt["current"],     "#8b949e",  "o",  90),
                ("Mín. varianza",    opt["min_var"],     _C_GREEN,   "D", 110),
                ("Máx. Sharpe",      opt["max_sharpe"],  "#f0883e",  "*", 190),
                ("Paridad de riesgo",opt["risk_parity"], "#d29922",  "s", 100),
            ]
            for label, stats, color, marker, sz in _portf_spec:
                ax.scatter(
                    stats["vol_pct"], stats["ret_pct"],
                    color=color, marker=marker, s=sz, zorder=5,
                    edgecolors="white", linewidths=0.6,
                    label=f"{label}  (Vol {stats['vol_pct']:.1f}%  Ret {stats['ret_pct']:.1f}%  SR {stats['sharpe']:.2f})",
                )
                ax.annotate(label, (stats["vol_pct"], stats["ret_pct"]),
                            textcoords="offset points", xytext=(7, 4),
                            fontsize=8, color=color)

            ax.axhline(rf, color="#6e7681", linewidth=0.8,
                       linestyle=":", alpha=0.6, label=f"Rf = {rf:.1f}%")

            ax.set_xlabel("Volatilidad anual (%)", color="#8b949e", fontsize=10)
            ax.set_ylabel("Retorno esperado (%)",  color="#8b949e", fontsize=10)
            ax.set_title("Frontera eficiente — Cartera optimizada",
                         color="#e6edf3", fontsize=12, pad=12)
            ax.tick_params(colors="#8b949e")
            for spine in ax.spines.values():
                spine.set_edgecolor("#30363d")
            ax.grid(True, color="#21262d", linewidth=0.5, alpha=0.8)
            legend = ax.legend(fontsize=7.5, framealpha=0.2, facecolor="#21262d",
                               edgecolor="#30363d", labelcolor="#c9d1d9",
                               loc="lower right")
            fig.tight_layout(pad=1.5)
            return _fig_to_response(fig)

    result = await asyncio.get_running_loop().run_in_executor(_executor, _make)
    if result is None:
        raise HTTPException(status_code=500)
    result.headers["Cache-Control"] = "public, max-age=300"
    return result


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

        # Pre-fetch históricos en paralelo para todos los tickers únicos
        unique_tickers = list({r[0] for r in rows})
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        price_cache = {}
        def _fetch_hist(t):
            try:
                return t, _get_ticker_hist(t, period="2y")["Close"]
            except Exception:
                return t, None
        with _TPE(max_workers=min(len(unique_tickers), 8)) as pool:
            for t, hist in pool.map(_fetch_hist, unique_tickers):
                price_cache[t] = hist

        # Agrupar por bucket
        results_raw = []
        for ticker, date_str, score in rows:
            if score is None:
                continue
            if score >= 15:
                bucket = "ALTA"
            elif score >= 8:
                bucket = "MEDIA"
            else:
                bucket = "BAJA"
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
                future_30 = idx + pd.Timedelta(days=30)
                future_90 = idx + pd.Timedelta(days=90)
                diffs2 = abs(hist.index - future_30)
                f_idx  = hist.index[diffs2.argmin()]
                if abs((f_idx - future_30).days) > 10:
                    continue
                p0 = float(hist.loc[idx])
                p1 = float(hist.loc[f_idx])
                ret_30 = (p1 - p0) / p0 * 100

                ret_90 = None
                try:
                    diffs3 = abs(hist.index - future_90)
                    f_idx3 = hist.index[diffs3.argmin()]
                    if abs((f_idx3 - future_90).days) <= 15:
                        p2 = float(hist.loc[f_idx3])
                        ret_90 = (p2 - p0) / p0 * 100
                except Exception:
                    pass

                # Max drawdown 30d period
                try:
                    period_prices = hist.loc[idx:f_idx]
                    if len(period_prices) > 1:
                        roll_max = period_prices.cummax()
                        dd_series = (period_prices - roll_max) / roll_max * 100
                        max_dd = float(dd_series.min())
                    else:
                        max_dd = None
                except Exception:
                    max_dd = None

                results_raw.append({
                    "bucket": bucket, "score": score,
                    "ret_30d": ret_30, "ret_90d": ret_90, "max_dd": max_dd,
                })
            except Exception:
                continue

        if not results_raw:
            return []

        df_bt = pd.DataFrame(results_raw)
        summary = []
        for b in ["ALTA", "MEDIA", "BAJA"]:
            sub = df_bt[df_bt["bucket"] == b]
            sub30 = sub["ret_30d"]
            if len(sub30) > 0:
                sub90_vals = sub["ret_90d"].dropna()
                avg_ret_90 = round(float(sub90_vals.mean()), 2) if len(sub90_vals) > 0 else None
                pct_pos_90 = round(float((sub90_vals > 0).mean() * 100), 1) if len(sub90_vals) > 0 else None

                avg_ret = float(sub30.mean())
                dd_vals = sub["max_dd"].dropna()
                avg_dd  = float(dd_vals.mean()) if len(dd_vals) > 0 else None
                calmar  = round(avg_ret / abs(avg_dd), 2) if (avg_dd and avg_dd < 0) else None

                summary.append({
                    "bucket":      b,
                    "n":           len(sub30),
                    "avg_ret":     round(avg_ret, 2),
                    "avg_ret_90":  avg_ret_90,
                    "med_ret":     round(float(sub30.median()), 2),
                    "pct_pos":     round(float((sub30 > 0).mean() * 100), 1),
                    "pct_pos_90":  pct_pos_90,
                    "calmar":      calmar,
                    "best":        round(float(sub30.max()), 2),
                    "worst":       round(float(sub30.min()), 2),
                })
        return summary

    summary = await asyncio.get_running_loop().run_in_executor(_executor, _compute)
    return templates.TemplateResponse("backtesting.html", {
        "request": request, "summary": summary,
    })


# ── Fiscalidad FIFO ────────────────────────────────────────────────────────────

@app.get("/fiscalidad", response_class=HTMLResponse)
async def fiscalidad_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    def _compute():
        from collections import defaultdict
        ops_all = get_operations(limit=10000, order_asc=True)
        # ops: id, ticker, date, type, shares, price_eur, notes, commission_eur

        # Agrupar compras por ticker (FIFO queue) y calcular ventas realizadas
        buy_queues = defaultdict(list)    # ticker -> list of (shares, price, commission)
        fifo_ops   = []
        annual_summary = defaultdict(lambda: {"gains": 0.0, "losses": 0.0, "commissions": 0.0})

        for op in ops_all:
            op_id, ticker, date_str, op_type, shares, price_eur, notes, commission = op
            year = date_str[:4] if date_str else "?"

            if op_type == "buy":
                buy_queues[ticker].append([shares, price_eur, commission])
            elif op_type == "sell":
                remaining = shares
                cost_basis = 0.0
                buy_commissions = 0.0
                sells_used = []
                q = buy_queues[ticker]
                while remaining > 0 and q:
                    head_shares, head_price, head_comm = q[0]
                    used = min(remaining, head_shares)
                    cost_basis    += used * head_price
                    buy_commissions += head_comm * (used / head_shares)
                    sells_used.append((used, head_price))
                    if used < head_shares:
                        q[0][0] -= used
                        q[0][2]  *= (head_shares - used) / head_shares
                    else:
                        q.pop(0)
                    remaining -= used

                proceeds   = shares * price_eur - commission
                total_cost = cost_basis + buy_commissions
                gain_loss  = proceeds - total_cost

                annual_summary[year]["commissions"] += commission + buy_commissions
                if gain_loss >= 0:
                    annual_summary[year]["gains"] += gain_loss
                else:
                    annual_summary[year]["losses"] += gain_loss

                fifo_ops.append({
                    "date":         date_str,
                    "ticker":       ticker,
                    "shares":       shares,
                    "sell_price":   price_eur,
                    "cost_basis":   round(total_cost, 4),
                    "commission":   round(commission + buy_commissions, 4),
                    "gain_loss":    round(gain_loss, 2),
                    "year":         year,
                })

        for year, data in annual_summary.items():
            data["net"] = round(data["gains"] + data["losses"], 2)
            data["gains"]       = round(data["gains"], 2)
            data["losses"]      = round(data["losses"], 2)
            data["commissions"] = round(data["commissions"], 2)

        # Plusvalías latentes (posiciones no realizadas)
        df = _read_csv()
        unrealized = []
        for ticker, remaining_buys in buy_queues.items():
            if not remaining_buys:
                continue
            total_shares = sum(b[0] for b in remaining_buys)
            total_cost   = sum(b[0] * b[1] for b in remaining_buys)
            avg_cost_per_share = total_cost / total_shares if total_shares else 0

            current_price = None
            if df is not None:
                row = df[df["ticker"] == ticker]
                if not row.empty:
                    p = row.iloc[0].get("price")
                    if p and not _is_nan(p):
                        current_price = float(p)

            gain_latent = None
            gain_pct    = None
            if current_price:
                gain_latent = (current_price - avg_cost_per_share) * total_shares
                gain_pct    = (current_price - avg_cost_per_share) / avg_cost_per_share * 100 if avg_cost_per_share else None

            unrealized.append({
                "ticker":        ticker,
                "shares":        round(total_shares, 4),
                "avg_cost":      round(avg_cost_per_share, 4),
                "current_price": current_price,
                "gain_latent":   round(gain_latent, 2) if gain_latent is not None else None,
                "gain_pct":      round(gain_pct, 2) if gain_pct is not None else None,
            })

        return fifo_ops, dict(annual_summary), unrealized

    fifo_ops, annual_summary, unrealized = await asyncio.get_running_loop().run_in_executor(_executor, _compute)
    return templates.TemplateResponse("fiscalidad.html", {
        "request":        request,
        "fifo_ops":       fifo_ops,
        "annual_summary": annual_summary,
        "unrealized":     unrealized,
        "commission_eur": _commission_eur(),
    })


# ── Monte Carlo ────────────────────────────────────────────────────────────────

@app.get("/montecarlo", response_class=HTMLResponse)
async def montecarlo_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    def _compute():
        import warnings
        warnings.filterwarnings("ignore")
        df        = _read_csv()
        positions = {r[0]: (r[1], r[2]) for r in _get_positions()}
        if df is None or not positions:
            return None

        values = {}
        vols   = {}
        rets   = {}
        for ticker, (shares, _) in positions.items():
            row = df[df["ticker"] == ticker]
            if row.empty:
                continue
            p = row.iloc[0].get("price")
            if not p or _is_nan(p):
                continue
            values[ticker] = shares * float(p)
            vol = row.iloc[0].get("volatility")
            if vol and not _is_nan(vol):
                vols[ticker] = float(vol) / 100.0
            else:
                vols[ticker] = 0.20
            mom = row.iloc[0].get("momentum_3m")
            if mom and not _is_nan(mom):
                rets[ticker] = float(mom) / 100.0 * (252 / 63)
            else:
                rets[ticker] = 0.07

        if not values:
            return None

        total    = sum(values.values())
        weights  = {t: v / total for t, v in values.items()}
        port_mu  = sum(weights[t] * rets.get(t, 0.07)  for t in weights)
        port_sig = sum(weights[t] * vols.get(t, 0.20)  for t in weights)

        N_PATHS  = 500
        rng      = np.random.default_rng(42)
        paths_1y  = np.zeros((N_PATHS, 253))
        paths_3y  = np.zeros((N_PATHS, 757))
        paths_1y[:, 0]  = total
        paths_3y[:, 0]  = total

        mu_d  = port_mu / 252
        sig_d = port_sig / np.sqrt(252)

        for t in range(1, 253):
            z = rng.standard_normal(N_PATHS)
            paths_1y[:, t] = paths_1y[:, t-1] * np.exp((mu_d - 0.5 * sig_d**2) + sig_d * z)
        for t in range(1, 757):
            z = rng.standard_normal(N_PATHS)
            paths_3y[:, t] = paths_3y[:, t-1] * np.exp((mu_d - 0.5 * sig_d**2) + sig_d * z)

        final_1y = paths_1y[:, 252]
        final_3y = paths_3y[:, 756]
        p10_1y  = float(np.percentile(final_1y, 10))
        p25_1y  = float(np.percentile(final_1y, 25))
        p50_1y  = float(np.percentile(final_1y, 50))
        p75_1y  = float(np.percentile(final_1y, 75))
        p90_1y  = float(np.percentile(final_1y, 90))
        p10_3y  = float(np.percentile(final_3y, 10))
        p50_3y  = float(np.percentile(final_3y, 50))
        p90_3y  = float(np.percentile(final_3y, 90))

        stats = {
            "total":   total,
            "p10_1y":  p10_1y,  "p25_1y": p25_1y, "p50_1y": p50_1y,
            "p75_1y":  p75_1y,  "p90_1y": p90_1y,
            "p10_3y":  p10_3y,  "p50_3y": p50_3y, "p90_3y": p90_3y,
            "port_mu": round(port_mu * 100, 2),
            "port_sig": round(port_sig * 100, 2),
        }
        return stats

    stats = await asyncio.get_running_loop().run_in_executor(_executor, _compute)
    return templates.TemplateResponse("montecarlo.html", {
        "request": request,
        "stats":   stats,
        "has_data": stats is not None,
    })


@app.get("/chart/montecarlo")
@limiter.limit("10/minute")
async def chart_montecarlo(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(403)

    def _make():
        import warnings
        warnings.filterwarnings("ignore")
        df        = _read_csv()
        positions = {r[0]: (r[1], r[2]) for r in _get_positions()}
        if df is None or not positions:
            return None

        values = {}
        vols   = {}
        rets   = {}
        for ticker, (shares, _) in positions.items():
            row = df[df["ticker"] == ticker]
            if row.empty:
                continue
            p = row.iloc[0].get("price")
            if not p or _is_nan(p):
                continue
            values[ticker] = shares * float(p)
            vol = row.iloc[0].get("volatility")
            vols[ticker] = float(vol) / 100.0 if (vol and not _is_nan(vol)) else 0.20
            mom = row.iloc[0].get("momentum_3m")
            rets[ticker] = float(mom) / 100.0 * (252 / 63) if (mom and not _is_nan(mom)) else 0.07

        if not values:
            return None

        total    = sum(values.values())
        weights  = {t: v / total for t, v in values.items()}
        port_mu  = sum(weights[t] * rets.get(t, 0.07)  for t in weights)
        port_sig = sum(weights[t] * vols.get(t, 0.20)  for t in weights)

        N_PATHS = 500
        rng     = np.random.default_rng(42)
        days    = 757
        paths   = np.zeros((N_PATHS, days))
        paths[:, 0] = total
        mu_d    = port_mu / 252
        sig_d   = port_sig / np.sqrt(252)

        for t in range(1, days):
            z = rng.standard_normal(N_PATHS)
            paths[:, t] = paths[:, t-1] * np.exp((mu_d - 0.5 * sig_d**2) + sig_d * z)

        x = np.arange(days)
        p10 = np.percentile(paths, 10, axis=0)
        p25 = np.percentile(paths, 25, axis=0)
        p50 = np.percentile(paths, 50, axis=0)
        p75 = np.percentile(paths, 75, axis=0)
        p90 = np.percentile(paths, 90, axis=0)

        fig, ax = plt.subplots(figsize=(10, 4.5))
        _style_ax(ax, fig)

        ax.fill_between(x, p10, p90, alpha=0.18, color=_C_BLUE, label="P10–P90")
        ax.fill_between(x, p25, p75, alpha=0.35, color=_C_BLUE, label="P25–P75")
        ax.plot(x, p50, color=_C_FG, linewidth=2, label="Mediana")
        ax.axhline(total, color=_C_TEXT, linewidth=0.8, linestyle="--", label="Valor inicial")
        ax.axvline(252, color=_C_GREEN, linewidth=0.8, linestyle=":", alpha=0.8)
        ax.axvline(756, color=_C_GREEN, linewidth=0.8, linestyle=":", alpha=0.8)
        ax.text(252, ax.get_ylim()[1] * 0.98, "1 año", color=_C_GREEN, fontsize=8, ha="center", va="top")
        ax.text(756, ax.get_ylim()[1] * 0.98, "3 años", color=_C_GREEN, fontsize=8, ha="center", va="top")

        ax.set_title("Monte Carlo — Simulación de cartera (500 paths, distribución log-normal)", fontsize=11, pad=8)
        ax.set_xlabel("Días", fontsize=9)
        ax.set_ylabel("Valor (€)", fontsize=9)
        ax.legend(fontsize=8, facecolor=_C_CARD, edgecolor=_C_GRID, labelcolor=_C_FG)
        fig.tight_layout()
        return fig

    fig = await asyncio.get_running_loop().run_in_executor(_executor, _make)
    if fig is None:
        raise HTTPException(404, "Sin datos suficientes")
    return _fig_to_response(fig)


# ── Stress Testing ─────────────────────────────────────────────────────────────

STRESS_SCENARIOS = [
    {"name": "Corrección moderada",   "desc": "Caída general de mercado",        "shock": -0.15, "sectors_shock": {}},
    {"name": "Bear market",           "desc": "Caída severa de mercado",          "shock": -0.35, "sectors_shock": {}},
    {"name": "Crash tecnológico",     "desc": "Caída del 50% en tecnología",      "shock": -0.10, "sectors_shock": {"Tecnología": -0.50}},
    {"name": "Crisis financiera",     "desc": "Caída del 40% en financieras",     "shock": -0.20, "sectors_shock": {"Financiero": -0.40}},
    {"name": "Rally de mercado",      "desc": "Subida general del 25%",           "shock": +0.25, "sectors_shock": {}},
]


@app.get("/stress-test", response_class=HTMLResponse)
async def stress_test_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    df        = _read_csv()
    positions = {r[0]: (r[1], r[2]) for r in _get_positions()}

    portfolio_data = []
    total_current = 0.0

    if df is not None:
        for ticker, (shares, avg) in positions.items():
            row = df[df["ticker"] == ticker]
            if row.empty:
                continue
            p = row.iloc[0].get("price")
            if not p or _is_nan(p):
                continue
            value  = shares * float(p)
            sector = row.iloc[0].get("block") or ""
            total_current += value
            portfolio_data.append({
                "ticker": ticker,
                "name":   row.iloc[0].get("name", ticker),
                "shares": shares,
                "price":  float(p),
                "value":  value,
                "sector": sector,
            })

    # Añadir escenario personalizado si está configurado
    active_scenarios = list(STRESS_SCENARIOS)
    try:
        _custom_name = get_setting("custom_stress_name") or ""
        _custom_pct_str = get_setting("custom_stress_pct") or ""
        if _custom_name.strip() and _custom_pct_str.strip():
            _custom_shock = float(_custom_pct_str) / 100
            active_scenarios.append({
                "name": _custom_name.strip(),
                "desc": "Escenario personalizado",
                "shock": _custom_shock,
                "sectors_shock": {},
            })
    except Exception:
        pass

    scenarios_results = []
    for scenario in active_scenarios:
        shock        = scenario["shock"]
        sec_shock    = scenario["sectors_shock"]
        total_new    = 0.0
        ticker_rows  = []
        for pos in portfolio_data:
            sector = pos["sector"]
            if sec_shock and sector in sec_shock:
                applied_shock = sec_shock[sector]
            else:
                applied_shock = shock
            new_value = pos["value"] * (1 + applied_shock)
            total_new += new_value
            ticker_rows.append({
                "ticker":       pos["ticker"],
                "name":         pos["name"],
                "value_before": pos["value"],
                "value_after":  new_value,
                "impact_eur":   new_value - pos["value"],
                "impact_pct":   applied_shock * 100,
            })
        impact_eur = total_new - total_current
        impact_pct = (impact_eur / total_current * 100) if total_current else 0
        scenarios_results.append({
            "name":          scenario["name"],
            "desc":          scenario["desc"],
            "total_before":  total_current,
            "total_after":   total_new,
            "impact_eur":    round(impact_eur, 2),
            "impact_pct":    round(impact_pct, 2),
            "tickers":       ticker_rows,
        })

    return templates.TemplateResponse("stress_test.html", {
        "request":           request,
        "scenarios":         scenarios_results,
        "total_current":     total_current,
        "has_data":          bool(portfolio_data),
    })


# ── Mover ticker de watchlist a portfolio ─────────────────────────────────────

@app.post("/tickers/move-to-portfolio")
@limiter.limit("10/minute")  # ISO 27001 A.12.2
async def move_to_portfolio(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    ticker:     str   = Form(...),
    shares:     float = Form(...),
    avg_price:  float = Form(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)
    t = ticker.strip().upper()
    if shares <= 0 or avg_price <= 0:
        return RedirectResponse("/tickers?tab=tickers&error=invalid", status_code=303)

    tickers = _load_tickers()
    watchlist = tickers.get("watchlist", {})
    portfolio = tickers.get("portfolio", {})

    if t not in watchlist:
        return RedirectResponse("/tickers?tab=tickers&error=not_in_watchlist", status_code=303)

    meta = watchlist.pop(t)
    portfolio[t] = meta
    tickers["watchlist"] = watchlist
    tickers["portfolio"] = portfolio
    _save_tickers(tickers)
    upsert_position(t, shares, avg_price)
    _invalidate_positions_cache()
    return RedirectResponse("/tickers?tab=tickers&saved=1", status_code=303)


# ── Treemap de cartera ─────────────────────────────────────────────────────────

def _squarify(values, x, y, w, h):
    """Algoritmo Slice-and-Dice simplificado para treemap."""
    if not values:
        return []
    total = sum(v for v, _ in values)
    if total <= 0:
        return []
    rects = []
    _slice_dice(values, x, y, w, h, total, True, rects)
    return rects


def _slice_dice(items, x, y, w, h, total, horizontal, rects):
    if not items:
        return
    if len(items) == 1:
        rects.append((x, y, w, h, items[0][1]))
        return
    # Split into two halves by area
    half = total / 2
    acc  = 0.0
    split_idx = 0
    for i, (val, _) in enumerate(items):
        acc += val
        split_idx = i
        if acc >= half:
            break
    left  = items[:split_idx + 1]
    right = items[split_idx + 1:]
    left_total  = sum(v for v, _ in left)
    right_total = sum(v for v, _ in right)
    if horizontal:
        split_w = w * left_total / total if total else w / 2
        _slice_dice(left,  x,            y, split_w,         h, left_total,  not horizontal, rects)
        _slice_dice(right, x + split_w,  y, w - split_w,     h, right_total, not horizontal, rects)
    else:
        split_h = h * left_total / total if total else h / 2
        _slice_dice(left,  x, y,            w, split_h,        left_total,  not horizontal, rects)
        _slice_dice(right, x, y + split_h,  w, h - split_h,    right_total, not horizontal, rects)


@app.get("/treemap", response_class=HTMLResponse)
async def treemap_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse("treemap.html", {"request": request})


@app.get("/chart/treemap")
async def chart_treemap(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        raise HTTPException(403)

    def _make():
        df        = _read_csv()
        positions = {r[0]: (r[1], r[2]) for r in _get_positions()}
        if df is None or not positions:
            return None

        items = []
        for ticker, (shares, avg) in positions.items():
            row = df[df["ticker"] == ticker]
            if row.empty:
                continue
            p = row.iloc[0].get("price")
            if not p or _is_nan(p):
                continue
            value  = shares * float(p)
            pnl    = (float(p) - float(avg)) / float(avg) * 100 if avg else 0
            name   = row.iloc[0].get("name", ticker)
            items.append((value, {"ticker": ticker, "name": name, "value": value, "pnl": pnl}))

        if not items:
            return None

        items.sort(key=lambda x: -x[0])
        rects = _squarify(items, 0, 0, 12, 8)

        fig, ax = plt.subplots(figsize=(12, 8))
        _style_ax(ax, fig)
        ax.set_xlim(0, 12)
        ax.set_ylim(0, 8)
        ax.set_aspect("auto")
        ax.axis("off")
        ax.set_title("Treemap de cartera — Tamaño: valor, Color: P&L%", fontsize=12, pad=8)

        for rx, ry, rw, rh, meta in rects:
            pnl = meta["pnl"]
            if pnl >= 10:
                color = "#1a5c2a"
            elif pnl >= 5:
                color = "#22863a"
            elif pnl >= 0:
                color = "#2ea043"
            elif pnl >= -5:
                color = "#b91c1c"
            elif pnl >= -10:
                color = "#991b1b"
            else:
                color = "#7f1d1d"

            rect = plt.Rectangle((rx + 0.02, ry + 0.02), rw - 0.04, rh - 0.04,
                                  facecolor=color, edgecolor="#30363d", linewidth=0.5)
            ax.add_patch(rect)
            cx, cy = rx + rw / 2, ry + rh / 2
            fs = max(5, min(11, int(rw * rh * 1.2)))
            ticker_text = meta["ticker"]
            ax.text(cx, cy + rh * 0.08, ticker_text,
                    ha="center", va="center", fontsize=fs,
                    fontweight="bold", color="#e6edf3")
            if rw > 1.0 and rh > 0.5:
                ax.text(cx, cy - rh * 0.15, f"€{meta['value']:,.0f}",
                        ha="center", va="center", fontsize=max(5, fs - 2), color="#c9d1d9")
                ax.text(cx, cy - rh * 0.38, f"{pnl:+.1f}%",
                        ha="center", va="center", fontsize=max(5, fs - 2),
                        color="#3fb950" if pnl >= 0 else "#f85149")

        fig.tight_layout()
        return fig

    fig = await asyncio.get_running_loop().run_in_executor(_executor, _make)
    if fig is None:
        raise HTTPException(404, "Sin posiciones")
    return _fig_to_response(fig)


# ── Endpoints de análisis IA on-demand ────────────────────────────────────────

@app.get("/ticker/{ticker}/analizar")
@limiter.limit("5/minute")
async def ticker_analizar(
    request: Request,
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
@limiter.limit("5/minute")
async def rebalanceo_sugerencia(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return JSONResponse({"error": "No autenticado"}, status_code=401)

    df        = _read_csv()
    positions = {row[0]: (row[1], row[2]) for row in _get_positions()}
    rows_data = []

    if df is not None:
        for row in df[df["category"] == "portfolio"].to_dict("records"):
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
@limiter.limit("5/minute")
async def noticias_analizar(request: Request, session: Optional[str] = Cookie(default=None)):
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
        for row in df.to_dict("records"):
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
    positions = {row[0]: (row[1], row[2]) for row in _get_positions()}
    rows = []
    if df is not None:
        for d in df[df["category"] == "portfolio"].to_dict("records"):
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
                "value_eur": round(value, 2) if value is not None else "",
                "pnl_pct": round(pnl, 2) if pnl is not None else "",
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
        headers={
            "Content-Disposition": "attachment; filename=portfolio.csv",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


@app.get("/export/watchlist")
async def export_watchlist(session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    df = _read_csv()
    rows = []
    if df is not None:
        for d in df[df["category"] == "watchlist"].sort_values("score", ascending=False).to_dict("records"):
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
        headers={
            "Content-Disposition": "attachment; filename=watchlist.csv",
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


# ── Importación masiva de tickers ─────────────────────────────────────────────

@app.post("/tickers/import")
@limiter.limit("2/minute")  # ISO 27001 A.12.2 — importación masiva costosa
async def tickers_import(
    request:    Request,
    session:    Optional[str] = Cookie(default=None),
    file:       UploadFile = File(...),
    csrf_token: Optional[str] = Form(default=None),
):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)
    _require_csrf(request, csrf_token)

    raw = await file.read()
    if len(raw) > 2 * 1024 * 1024:  # 2 MB máximo (ISO 27001 A.12.2 — protección DoS)
        return RedirectResponse("/tickers?error=archivo_muy_grande", status_code=303)
    content = raw.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))
    tickers_data = _load_tickers()
    imported = 0
    errors = []
    for i, row in enumerate(reader, start=2):
        ticker = (row.get("ticker") or "").strip().upper()
        categoria = (row.get("categoria") or "watchlist").strip().lower()
        nombre = (row.get("nombre") or row.get("name") or ticker).strip()[:_MAX_NAME_LEN]
        bloque = (row.get("bloque") or row.get("block") or "").strip()[:_MAX_BLOCK_LEN]
        region = (row.get("region") or "").strip()[:_MAX_REGION_LEN]
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
    log_audit_event("tickers_imported", ip_address=get_remote_address(request), details=f"count={imported},errors={len(errors)}")
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


@app.get("/robots.txt")
async def robots_txt():
    """ISO 27001 A.5 — evita indexación del dashboard privado por motores de búsqueda."""
    return Response(
        content="User-agent: *\nDisallow: /\n",
        media_type="text/plain",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/sw.js")
async def service_worker():
    return Response(
        content=_SW_JS,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=0",
            "Service-Worker-Allowed": "/",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'",
        },
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
@limiter.limit("10/minute")
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
    # Validar longitudes (ISO 27001 A.14.2 — prevenir payloads excesivos)
    if len(endpoint) > 500 or len(p256dh) > 500 or len(auth) > 500:
        raise HTTPException(status_code=400, detail="payload demasiado grande")
    try:
        _parsed = urlparse(endpoint)
        if _parsed.scheme != "https" or not _parsed.netloc:
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="endpoint inválido")
    ua = request.headers.get("user-agent", "")[:200]
    upsert_push_subscription(endpoint, p256dh, auth, ua)
    log_audit_event("push_subscribed", ip_address=get_remote_address(request), details=f"ua={ua[:80]}")
    return JSONResponse({"ok": True})


@app.post("/push/unsubscribe")
@limiter.limit("10/minute")  # ISO 27001 A.12.2
async def push_unsubscribe(
    request: Request,
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        raise HTTPException(status_code=401)
    # ISO 27001 A.14.2 — validar Content-Type antes de parsear JSON (evita content-type confusion)
    ct = request.headers.get("content-type", "")
    if not ct.startswith("application/json"):
        raise HTTPException(status_code=415, detail="Content-Type debe ser application/json")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400)
    if not _validate_csrf(body.get("csrf_token")):
        raise HTTPException(status_code=403, detail="CSRF inválido")
    endpoint = body.get("endpoint", "")
    if endpoint:
        delete_push_subscription(endpoint)
        _ep_hash = hashlib.sha256(endpoint.encode()).hexdigest()[:16]
        log_audit_event("push_unsubscribed", ip_address=get_remote_address(request), details=f"endpoint_hash={_ep_hash}")
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
    log_audit_event("push_test_sent", ip_address=get_remote_address(request), details=f"sent={sent}")
    return JSONResponse({"sent": sent})


# ── Recomendaciones de mercado ────────────────────────────────────────────────

_discovery_lock = threading.Lock()
_discovery_running = False


@app.get("/recomendaciones", response_class=HTMLResponse)
async def recomendaciones_page(request: Request, session: Optional[str] = Cookie(default=None)):
    if not _is_auth(session):
        return RedirectResponse("/login", status_code=302)

    rows        = get_discoveries()
    generated   = get_discoveries_generated_at()
    stale       = is_stale()
    universe_n  = len(get_universe()) if _DISCOVERY_AVAILABLE else 0

    by_horizon = {"largo": [], "medio": [], "corto": []}
    for r in rows:
        h = r.get("horizon", "medio")
        if h in by_horizon:
            by_horizon[h].append(r)

    return templates.TemplateResponse("recomendaciones.html", {
        "request":    request,
        "by_horizon": by_horizon,
        "generated":  generated,
        "stale":      stale,
        "universe_n": universe_n,
        "has_data":   bool(rows),
        "running":    _discovery_running,
    })


@app.post("/recomendaciones/refresh")
@limiter.limit("2/minute")
async def recomendaciones_refresh(request: Request, session: Optional[str] = Cookie(default=None)):
    global _discovery_running
    if not _is_auth(session):
        raise HTTPException(403)
    form = await request.form()
    _require_csrf(request, form.get("csrf_token"))

    if not _DISCOVERY_AVAILABLE:
        return RedirectResponse("/recomendaciones?error=not_available", status_code=303)

    # Evitar ejecuciones simultáneas
    if not _discovery_lock.acquire(blocking=False):
        return RedirectResponse("/recomendaciones?info=running", status_code=303)

    _discovery_running = True

    def _run():
        global _discovery_running
        try:
            generate_discoveries()
        except Exception:
            logger.exception("Error generando recomendaciones")
        finally:
            _discovery_running = False
            _discovery_lock.release()

    _executor.submit(_run)
    return RedirectResponse("/recomendaciones?info=started", status_code=303)


@app.post("/recomendaciones/add-to-watchlist")
@limiter.limit("10/minute")  # ISO 27001 A.12.2
async def recomendaciones_add_watchlist(
    request: Request,
    session: Optional[str] = Cookie(default=None),
):
    if not _is_auth(session):
        raise HTTPException(403)
    form = await request.form()
    _require_csrf(request, form.get("csrf_token"))

    ticker  = str(form.get("ticker", "")).strip().upper()
    name    = str(form.get("name", "")).strip()[:80]
    sector  = str(form.get("sector", "")).strip()[:60]
    horizon = str(form.get("horizon", "medio")).strip()
    region  = str(form.get("region", "")).strip()[:40]

    if not ticker:
        raise HTTPException(400, "ticker requerido")
    if horizon not in ("largo", "medio", "corto"):
        horizon = "medio"

    upsert_ticker(ticker, "watchlist", name=name or None, block=sector or None,
                  region=region or None, horizon=horizon)
    log_audit_event("ticker_added", ip_address=get_remote_address(request), details=f"ticker={ticker},categoria=watchlist,origen=recomendaciones")
    return RedirectResponse("/recomendaciones?added=" + ticker, status_code=303)


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("WEB_PORT", "8589")))
