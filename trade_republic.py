"""
Integración con Trade Republic via pytr.
Librería enlazada al repo oficial: https://github.com/pytr-org/pytr

Autenticación en dos pasos:
  1. setup_device()       → genera clave ECDSA, solicita SMS a TR
  2. complete_setup(code) → confirma con el código SMS, guarda keyfile

Sincronización (una sola conexión WebSocket):
  sync_positions() → (positions, cash_eur, transactions)

Historial del depósito (conexión independiente):
  get_portfolio_history(timeframe) → lista de puntos {time_ms, value}
"""

import asyncio
import hashlib
import json
import logging
import os
import pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

logger = logging.getLogger(__name__)

TR_PHONE   = os.getenv("TR_PHONE", "")
TR_PIN     = os.getenv("TR_PIN", "")
TR_KEYFILE = os.getenv("TR_KEYFILE", "data/tr_keyfile.pem")

_SETUP_STATE_FILE = pathlib.Path("data/tr_setup.json")


# ── Estado del módulo ──────────────────────────────────────────────────────────

def is_configured() -> bool:
    """True si TR_PHONE y TR_PIN están definidos en el entorno."""
    return bool(TR_PHONE and TR_PIN)


def is_setup() -> bool:
    """True si el keyfile de TR existe (dispositivo ya vinculado)."""
    return pathlib.Path(TR_KEYFILE).exists()


def has_pending_setup() -> bool:
    """True si hay un setup iniciado esperando el código SMS."""
    return _SETUP_STATE_FILE.exists()


# ── Mapa ISIN → ticker ────────────────────────────────────────────────────────

def _build_isin_map() -> dict:
    """
    Construye el mapa ISIN → ticker con dos fuentes (por orden de prioridad):

    1. Sección `tr_isin_map` en tickers.yaml (manual, máxima prioridad):
         tr_isin_map:
           US0378331005: AAPL
           DE0005140008: DBK.DE

    2. Auto-match via yfinance: consulta yf.Ticker(t).isin para cada ticker
       conocido en tickers.yaml en paralelo (timeout total 20 s).
       No sobreescribe entradas manuales.
    """
    import yfinance as yf

    isin_map: dict = {}
    try:
        with open("tickers.yaml") as f:
            data = yaml.safe_load(f) or {}

        # 1. Overrides manuales
        isin_map.update(data.get("tr_isin_map", {}))

        # 2. Auto-match via yfinance para tickers no cubiertos por el mapa manual
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
                            logger.debug(f"TR auto-mapeado: {isin} → {ticker}")
                    except Exception:
                        pass

    except Exception as e:
        logger.warning(f"Error construyendo isin_map: {e}")

    return isin_map


# ── Core async ────────────────────────────────────────────────────────────────

async def _async_fetch_portfolio() -> tuple:
    """
    Abre una sola conexión WebSocket a TR y descarga en paralelo:
    - Portfolio compacto (posiciones: ISIN, shares, averageBuyIn)
    - Saldo de cuenta (EUR)
    - Últimas transacciones (timeline)

    Después resuelve nombres via instrument_details para los ISINs del portfolio.

    Retorna (positions, cash_eur, transactions)
    """
    from pytr.api import TradeRepublicApi

    # El mapa ISIN→ticker se construye en un executor para no bloquear el loop
    isin_map = await asyncio.get_running_loop().run_in_executor(None, _build_isin_map)

    tr = TradeRepublicApi(phone_no=TR_PHONE, pin=TR_PIN, keyfile=TR_KEYFILE)
    tr.login()

    # Suscribirse a las 3 fuentes de datos simultáneamente
    await tr.compact_portfolio()
    await tr.cash()
    await tr.timeline_transactions()

    positions_raw = []
    cash_eur = None
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

    # Resolver nombre (shortName) de cada posición via instrument_details
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

    # Cerrar WebSocket
    if tr._ws and tr._ws.close_code is None:
        await tr._ws.close()

    # ── Construir posiciones ───────────────────────────────────────────────────
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

    # ── Normalizar transacciones ───────────────────────────────────────────────
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
        if ts:
            try:
                import datetime
                date_str = datetime.datetime.fromtimestamp(
                    int(ts) / 1000, tz=datetime.timezone.utc
                ).strftime("%d/%m/%Y")
            except Exception:
                pass

        transactions.append({
            "id":        item.get("id", ""),
            "title":     item.get("title", "—"),
            "body":      item.get("body", ""),
            "amount":    amount_val,
            "timestamp": int(ts) if ts else None,
            "date":      date_str,
        })

    return positions, cash_eur, transactions


async def _async_portfolio_history(timeframe: str) -> list:
    """
    Obtiene el historial de valor del depósito TR.
    Abre una conexión independiente (no mezcla con sync_positions).

    Retorna lista de dicts {time_ms: int, value: float}.
    """
    from pytr.api import TradeRepublicApi

    tr = TradeRepublicApi(phone_no=TR_PHONE, pin=TR_PIN, keyfile=TR_KEYFILE)
    tr.login()

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


# ── API pública ───────────────────────────────────────────────────────────────

def sync_positions() -> tuple:
    """
    Sincroniza posiciones desde Trade Republic en una sola conexión WebSocket.

    Retorna (positions, cash_eur, transactions):
      - positions: list[dict]  → {isin, name, ticker, shares, avg_price, matched}
      - cash_eur:  float|None  → saldo disponible en EUR
      - transactions: list[dict] → últimas operaciones {id, title, body, amount, date, timestamp}

    Lanza ValueError si TR no está configurado o el keyfile no existe.
    """
    if not is_configured():
        raise ValueError("TR_PHONE y TR_PIN no están configurados en .env")
    if not is_setup():
        raise ValueError(
            "Dispositivo no vinculado. Ejecuta /tr_setup o usa la sección "
            "Trade Republic en Posiciones."
        )
    return asyncio.run(_async_fetch_portfolio())


def get_portfolio_history(timeframe: str = "1y") -> list:
    """
    Obtiene el historial de valor del depósito en TR.
    timeframe: "1d" | "1w" | "1m" | "3m" | "6m" | "1y" | "max"

    Retorna lista de {time_ms, value} ordenada cronológicamente.
    Lanza ValueError si TR no está listo.
    """
    if not is_configured():
        raise ValueError("TR_PHONE y TR_PIN no están configurados en .env")
    if not is_setup():
        raise ValueError("Dispositivo no vinculado.")
    return asyncio.run(_async_portfolio_history(timeframe))


# ── Setup inicial ─────────────────────────────────────────────────────────────

def setup_device() -> str:
    """
    Paso 1: genera clave ECDSA y solicita SMS a TR.
    Guarda estado en data/tr_setup.json para complete_setup().
    """
    from pytr.api import TradeRepublicApi

    if not is_configured():
        raise ValueError("TR_PHONE y TR_PIN no están configurados en .env")

    tr = TradeRepublicApi(phone_no=TR_PHONE, pin=TR_PIN, keyfile=TR_KEYFILE)
    try:
        tr.initiate_device_reset()
    except KeyError:
        import requests as _req
        # Repetir la llamada para capturar la respuesta real de TR
        r = _req.post(
            "https://api.traderepublic.com/api/v1/auth/account/reset/device",
            json={"phoneNumber": TR_PHONE, "pin": TR_PIN},
            headers={"User-Agent": "TradeRepublic/Android 30/App Version 1.1.5534"},
        )
        raise ValueError(
            f"Trade Republic rechazó la solicitud (HTTP {r.status_code}): {r.text[:200]}"
        )

    _SETUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETUP_STATE_FILE.write_text(json.dumps({
        "process_id": tr._process_id,
        "sk_pem":     tr.sk.to_pem().decode("ascii"),
    }))

    return f"SMS enviado a {TR_PHONE}. Introduce el código para completar la vinculación."


def complete_setup(code: str) -> str:
    """
    Paso 2: confirma con el código SMS y guarda el keyfile.
    """
    from pytr.api import TradeRepublicApi
    from ecdsa import SigningKey

    if not _SETUP_STATE_FILE.exists():
        raise ValueError(
            "No hay setup pendiente. Ejecuta primero el inicio de configuración."
        )

    state = json.loads(_SETUP_STATE_FILE.read_text())

    tr = TradeRepublicApi(phone_no=TR_PHONE, pin=TR_PIN, keyfile=TR_KEYFILE)
    tr._process_id = state["process_id"]
    tr.sk = SigningKey.from_pem(state["sk_pem"].encode(), hashfunc=hashlib.sha512)

    pathlib.Path(TR_KEYFILE).parent.mkdir(parents=True, exist_ok=True)
    tr.complete_device_reset(code)

    _SETUP_STATE_FILE.unlink(missing_ok=True)

    if not pathlib.Path(TR_KEYFILE).exists():
        raise ValueError("Código incorrecto o expirado. Reinicia el proceso de configuración.")

    return f"Trade Republic vinculado correctamente. Keyfile guardado en {TR_KEYFILE}."
