"""
Integración con Trade Republic via pytr.
Librería enlazada al repo oficial: https://github.com/pytr-org/pytr

Autenticación via web login (User-Agent Chrome, no versión de app Android):
  1. setup_device()       → solicita código de 4 dígitos en la app TR
  2. complete_setup(code) → confirma con el código, guarda cookies de sesión

Sincronización (una sola conexión WebSocket):
  sync_positions() → (positions, cash_eur, transactions)

Historial del depósito (conexión independiente):
  get_portfolio_history(timeframe) → lista de puntos {time_ms, value}
"""

import asyncio
import json
import logging
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

TR_PHONE        = os.getenv("TR_PHONE", "")
TR_PIN          = os.getenv("TR_PIN", "")
TR_COOKIES_FILE = os.getenv("TR_COOKIES_FILE", "data/tr_cookies.txt")

_SETUP_STATE_FILE = pathlib.Path("data/tr_setup.json")


# ── Estado del módulo ──────────────────────────────────────────────────────────

def is_configured() -> bool:
    return bool(TR_PHONE and TR_PIN)


def is_setup() -> bool:
    """True si el fichero de cookies existe (sesión web activa)."""
    return pathlib.Path(TR_COOKIES_FILE).exists()


def has_pending_setup() -> bool:
    """True si hay un setup iniciado esperando el código de la app TR."""
    return _SETUP_STATE_FILE.exists()


# ── Mapa ISIN → ticker ────────────────────────────────────────────────────────

def _build_isin_map() -> dict:
    """
    Construye el mapa ISIN → ticker con dos fuentes (por orden de prioridad):

    1. Tabla tr_isin_map en BD (mapeos manuales, máxima prioridad).
    2. Auto-match via yfinance: consulta yf.Ticker(t).isin para cada ticker
       de la BD que aún no tenga ISIN mapeado (timeout total 20 s).
    """
    import yfinance as yf
    from database import get_tr_isin_map, get_tickers_as_yaml_dict

    isin_map: dict = {}
    try:
        isin_map.update(get_tr_isin_map())

        data = get_tickers_as_yaml_dict()
        existing_tickers = set(isin_map.values())
        candidates = [
            t for cat in ("portfolio", "watchlist")
            for t in data.get(cat, {})
            if t not in existing_tickers
        ]

        if candidates:
            def _get_isin(ticker: str):
                try:
                    isin = yf.Ticker(ticker).isin
                    if isin and str(isin) not in ("-", "None", "nan", ""):
                        return str(isin), ticker
                except Exception:
                    pass
                return None, ticker

            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_get_isin, t): t for t in candidates}
                for fut in as_completed(futures, timeout=20):
                    try:
                        isin, ticker = fut.result(timeout=3)
                        if isin and isin not in isin_map:
                            isin_map[isin] = ticker
                            logger.debug("TR auto-mapeado: %s → %s", isin, ticker)
                    except Exception:
                        pass

    except Exception as e:
        logger.warning("Error construyendo isin_map: %s", e)

    return isin_map


# ── Helper: crear instancia TR con web session ─────────────────────────────────

def _make_tr():
    from pytr.api import TradeRepublicApi
    return TradeRepublicApi(
        phone_no=TR_PHONE,
        pin=TR_PIN,
        save_cookies=True,
        cookies_file=TR_COOKIES_FILE,
    )


# ── Core async ────────────────────────────────────────────────────────────────

async def _async_fetch_portfolio() -> tuple:
    """
    Abre una sola conexión WebSocket a TR y descarga en paralelo:
    portfolio + cash + transacciones. Usa sesión web (cookies).
    """
    isin_map = await asyncio.get_running_loop().run_in_executor(None, _build_isin_map)

    tr = _make_tr()
    if not tr.resume_websession():
        raise ValueError(
            "Sesión TR expirada o no válida. Vuelve a vincular el dispositivo."
        )

    await tr.compact_portfolio()
    await tr.cash()
    await tr.timeline_transactions()

    positions_raw   = []
    cash_eur        = None
    transactions_raw = []
    pending = 3

    while pending > 0:
        sub_id, sub, response = await tr.recv()
        stype = sub.get("type")

        if stype == "compactPortfolio":
            positions_raw = response.get("positions", [])
            pending -= 1
        elif stype == "cash":
            items = response if isinstance(response, list) else []
            for item in items:
                if item.get("currencyId") == "EUR":
                    cash_eur = float(item["amount"])
                    break
            pending -= 1
        elif stype == "timelineTransactions":
            transactions_raw = response.get("items", []) if isinstance(response, dict) else []
            pending -= 1

        await tr.unsubscribe(sub_id)

    # Resolver shortName para cada posición
    detail_subs: dict = {}
    for pos in positions_raw:
        sub_id = await tr.instrument_details(pos["instrumentId"])
        detail_subs[sub_id] = pos

    while detail_subs:
        sub_id, sub, response = await tr.recv()
        if sub.get("type") == "instrument" and sub_id in detail_subs:
            pos = detail_subs.pop(sub_id)
            pos["name"] = response.get("shortName", pos["instrumentId"])
            await tr.unsubscribe(sub_id)

    if tr._ws and tr._ws.close_code is None:
        await tr._ws.close()

    # Construir posiciones
    positions = []
    for pos in positions_raw:
        shares = float(pos.get("netSize", 0))
        if shares <= 0:
            continue
        isin      = pos["instrumentId"]
        avg_price = float(pos.get("averageBuyIn", 0))
        name      = pos.get("name", isin)
        ticker    = isin_map.get(isin)
        positions.append({
            "isin":      isin,
            "name":      name,
            "ticker":    ticker,
            "shares":    shares,
            "avg_price": avg_price,
            "matched":   ticker is not None,
        })

    # Normalizar transacciones
    transactions = []
    for item in transactions_raw[:50]:
        ts = item.get("timestamp") or item.get("time")
        amount_obj = item.get("amount", {})
        amount_val = None
        try:
            if isinstance(amount_obj, dict):
                amount_val = float(amount_obj.get("value", 0))
            elif amount_obj is not None:
                amount_val = float(amount_obj)
        except (TypeError, ValueError):
            pass

        date_str = "—"
        ts_ms = None
        if ts:
            try:
                import datetime
                if isinstance(ts, str):
                    # ISO format: '2026-03-10T13:47:55.620 0000' (space instead of +)
                    ts_clean = ts.replace(" ", "+")
                    dt = datetime.datetime.fromisoformat(ts_clean)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    ts_ms = int(dt.timestamp() * 1000)
                else:
                    ts_ms = int(ts)
                date_str = datetime.datetime.fromtimestamp(
                    ts_ms / 1000, tz=datetime.timezone.utc
                ).strftime("%d/%m/%Y")
            except Exception:
                pass

        transactions.append({
            "id":        item.get("id", ""),
            "title":     item.get("title", "—"),
            "body":      item.get("body", ""),
            "amount":    amount_val,
            "timestamp": ts_ms,
            "date":      date_str,
        })

    return positions, cash_eur, transactions


async def _async_portfolio_history(timeframe: str) -> list:
    tr = _make_tr()
    if not tr.resume_websession():
        raise ValueError("Sesión TR expirada. Vuelve a vincular el dispositivo.")

    await tr.portfolio_history(timeframe)
    _, _, response = await asyncio.wait_for(tr.recv(), timeout=15)

    if tr._ws and tr._ws.close_code is None:
        await tr._ws.close()

    raw_items = response.get("items", []) if isinstance(response, dict) else []
    result = []
    for item in raw_items:
        try:
            t = item.get("time")
            v = item.get("value")
            if t is not None and v is not None:
                result.append({"time_ms": int(t), "value": float(v)})
        except (TypeError, ValueError):
            pass

    return result


# ── Resolución automática ISIN → ticker via OpenFIGI ─────────────────────────

_OPENFIGI_SUFFIX = {
    "SW": ".SW", "FP": ".PA", "LN": ".L",  "DC": ".CO", "AT": ".AX",
    "GR": ".DE", "NA": ".AS", "IM": ".MI", "SM": ".MC", "SS": ".ST",
    "FH": ".HE", "PL": ".LS", "BB": ".BR", "NO": ".OL", "PW": ".WA",
}
_OPENFIGI_US = {"US", "UN", "UW", "UA", "UR"}

# Prefijo de país del ISIN → exchCode preferido en OpenFIGI
_ISIN_COUNTRY_TO_EXCH = {
    "CH": "SW", "DK": "DC", "GB": "LN", "FR": "FP", "AU": "AT",
    "SE": "SS", "NL": "NA", "DE": "GR", "IT": "IM", "ES": "SM",
    "FI": "FH", "PT": "PL", "BE": "BB", "NO": "NO", "PL": "PW",
}

# ISINs con ticker conocido — evita depender de OpenFIGI para estos casos
_TR_KNOWN_ISINS = {
    # Crypto TR-specific (OpenFIGI no los conoce)
    "XF000BTC0017": "BTC-EUR",
    "XF000ETH0017": "ETH-USD",
    # Acciones europeas que OpenFIGI resuelve mal consistentemente
    "CH0012032113": "ROG.SW",    # Roche
    "CH0038863350": "NESN.SW",   # Nestlé
    "DK0062498333": "NOVO-B.CO", # Novo-Nordisk B
    "GB00B2B0DG97": "REL.L",     # RELX
}


def resolve_isins_openfigi(isins: list) -> dict:
    """
    Consulta OpenFIGI (gratuito, sin clave) para resolver ISINs a tickers yfinance.
    Devuelve {isin: ticker} solo para los que se resuelven correctamente.
    Se procesan en lotes de 10 (límite de la API).
    """
    import requests
    import yfinance as yf

    result = {}

    # Resolver ISINs conocidos de TR (crypto) sin llamar a OpenFIGI
    for isin in isins:
        if isin in _TR_KNOWN_ISINS:
            result[isin] = _TR_KNOWN_ISINS[isin]
            logger.info(f"TR known: {isin} → {_TR_KNOWN_ISINS[isin]}")

    # Resto: excluir XF (TR-specific) y los ya resueltos
    lookups = [i for i in isins if not i.startswith("XF") and i not in result]

    for batch_start in range(0, len(lookups), 10):
        batch = lookups[batch_start:batch_start + 10]
        payload = [{"idType": "ID_ISIN", "idValue": isin} for isin in batch]
        try:
            resp = requests.post(
                "https://api.openfigi.com/v3/mapping",
                json=payload,
                timeout=10,
            )
            if resp.status_code != 200:
                continue
            for isin, entry in zip(batch, resp.json()):
                items = entry.get("data", [])
                if not items:
                    continue
                # Filtrar a acciones (Common Stock)
                equities = [
                    i for i in items
                    if i.get("securityType2") in ("Common Stock", "Equity Shares")
                ] or items
                # Preferir la bolsa del país del ISIN (CH→SW, DK→DC, GB→LN, etc.)
                preferred_exch = _ISIN_COUNTRY_TO_EXCH.get(isin[:2], "")
                ranked = sorted(
                    equities,
                    key=lambda i: (
                        0 if i.get("exchCode", "") == preferred_exch else
                        1 if i.get("exchCode", "") in _OPENFIGI_SUFFIX else
                        2
                    )
                )
                # Probar candidatos en orden hasta que yfinance confirme el ticker
                for item in ranked:
                    exch   = item.get("exchCode", "")
                    ticker = item.get("ticker", "")
                    if not ticker:
                        continue
                    suffix = _OPENFIGI_SUFFIX.get(exch, "" if exch in _OPENFIGI_US else None)
                    if suffix is None:
                        continue
                    candidate = ticker + suffix
                    try:
                        price = yf.Ticker(candidate).fast_info.get("lastPrice")
                        if price:
                            result[isin] = candidate
                            logger.info(f"OpenFIGI+yf: {isin} → {candidate} (exch={exch})")
                            break
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"OpenFIGI error en lote: {e}")

    return result


# ── API pública ───────────────────────────────────────────────────────────────

def sync_positions() -> tuple:
    """
    Sincroniza posiciones desde Trade Republic.
    Retorna (positions, cash_eur, transactions).
    """
    if not is_configured():
        raise ValueError("TR_PHONE y TR_PIN no están configurados en .env")
    if not is_setup():
        raise ValueError(
            "Dispositivo no vinculado. Usa la sección Trade Republic en Posiciones."
        )
    return asyncio.run(_async_fetch_portfolio())


def get_portfolio_history(timeframe: str = "1y") -> list:
    """
    Historial de valor del depósito.
    timeframe: "1d" | "1w" | "1m" | "3m" | "6m" | "1y" | "max"
    """
    if not is_configured():
        raise ValueError("TR_PHONE y TR_PIN no están configurados.")
    if not is_setup():
        raise ValueError("Dispositivo no vinculado.")
    return asyncio.run(_async_portfolio_history(timeframe))


# ── Setup inicial (web login) ──────────────────────────────────────────────────

def setup_device() -> str:
    """
    Paso 1: solicita un código de verificación en la app Trade Republic.
    El usuario verá un código de 4 dígitos en su móvil.
    """
    if not is_configured():
        raise ValueError("TR_PHONE y TR_PIN no están configurados en .env")

    tr = _make_tr()
    try:
        countdown = tr.initiate_weblogin()
    except ValueError as e:
        raise ValueError(f"Trade Republic rechazó la solicitud: {e}")

    _SETUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETUP_STATE_FILE.write_text(json.dumps({"process_id": tr._process_id}))

    return (
        f"Abre la app Trade Republic en tu móvil. "
        f"Verás un código de 4 dígitos para confirmar el acceso. "
        f"Tienes {countdown} segundos para introducirlo."
    )


def complete_setup(code: str) -> str:
    """
    Paso 2: confirma con el código de la app TR y guarda la sesión.
    """
    if not _SETUP_STATE_FILE.exists():
        raise ValueError(
            "No hay setup pendiente. Ejecuta primero el inicio de configuración."
        )

    state = json.loads(_SETUP_STATE_FILE.read_text())

    pathlib.Path(TR_COOKIES_FILE).parent.mkdir(parents=True, exist_ok=True)
    tr = _make_tr()
    tr._process_id = state["process_id"]

    try:
        tr.complete_weblogin(code.strip())
    except Exception as e:
        raise ValueError(f"Código incorrecto o expirado: {e}")

    _SETUP_STATE_FILE.unlink(missing_ok=True)

    if not pathlib.Path(TR_COOKIES_FILE).exists():
        raise ValueError("No se pudo guardar la sesión. Reinicia el proceso.")

    return "Trade Republic vinculado correctamente. Sesión guardada."
