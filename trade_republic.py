"""
Integración con Trade Republic via pytr.
Librería enlazada al repo oficial: https://github.com/pytr-org/pytr

Autenticación en dos pasos:
  1. setup_device()      → genera clave ECDSA, solicita SMS a TR
  2. complete_setup(code)→ confirma con el código SMS, guarda keyfile

Sincronización:
  sync_positions() → lista de posiciones + saldo de caja
"""

import asyncio
import hashlib
import json
import logging
import os
import pathlib

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


# ── Construcción del mapa ISIN → ticker ───────────────────────────────────────

def _build_isin_map() -> dict:
    """
    Lee la sección `tr_isin_map` de tickers.yaml.
    Formato:
        tr_isin_map:
          US0378331005: AAPL
          DE0005140008: DBK.DE

    El mapeo manual es la única fuente para mantener la sincronización rápida.
    Los ISINs sin mapeo se reportan al usuario para que los añada.
    """
    try:
        with open("tickers.yaml") as f:
            data = yaml.safe_load(f) or {}
        return dict(data.get("tr_isin_map", {}))
    except Exception as e:
        logger.warning(f"No se pudo cargar tr_isin_map de tickers.yaml: {e}")
        return {}


# ── Core async ────────────────────────────────────────────────────────────────

async def _async_fetch_portfolio() -> tuple:
    """
    Conecta a Trade Republic vía WebSocket y descarga:
    - Posiciones del portfolio (ISIN, nombre, acciones, precio medio)
    - Saldo en cuenta (EUR)

    Retorna (positions: list[dict], cash_eur: float | None)
    """
    from pytr.api import TradeRepublicApi

    isin_map = _build_isin_map()

    tr = TradeRepublicApi(phone_no=TR_PHONE, pin=TR_PIN, keyfile=TR_KEYFILE)
    tr.login()

    # Suscribirse a portfolio compacto y saldo simultáneamente
    await tr.compact_portfolio()
    await tr.cash()

    positions_raw = []
    cash_eur = None
    pending = 2

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
        await tr.unsubscribe(sub_id)

    # Obtener nombre de cada posición (instrument_details)
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

    # Construir resultado
    results = []
    for pos in positions_raw:
        shares = float(pos.get("netSize", 0))
        if shares <= 0:
            continue
        isin      = pos["instrumentId"]
        avg_price = float(pos.get("averageBuyIn", 0))
        name      = pos.get("name", isin)
        ticker    = isin_map.get(isin)
        results.append({
            "isin":      isin,
            "name":      name,
            "ticker":    ticker,
            "shares":    shares,
            "avg_price": avg_price,
            "matched":   ticker is not None,
        })

    return results, cash_eur


# ── API pública ───────────────────────────────────────────────────────────────

def sync_positions() -> tuple:
    """
    Sincroniza posiciones desde Trade Republic.
    Retorna (positions: list[dict], cash_eur: float | None).

    Cada posición: {isin, name, ticker, shares, avg_price, matched}
      - matched=True  → ticker mapeado en tr_isin_map; listo para upsert
      - matched=False → ISIN sin mapeo; hay que añadirlo a tr_isin_map en tickers.yaml

    Lanza ValueError si TR no está configurado o el keyfile no existe.
    """
    if not is_configured():
        raise ValueError("TR_PHONE y TR_PIN no están configurados en .env")
    if not is_setup():
        raise ValueError(
            "Dispositivo no vinculado. Ejecuta /tr_setup o usa la sección Trade Republic en Posiciones."
        )
    return asyncio.run(_async_fetch_portfolio())


def setup_device() -> str:
    """
    Paso 1: genera clave ECDSA y solicita SMS a TR.
    Guarda estado en data/tr_setup.json para complete_setup().
    Retorna mensaje informativo.
    """
    from pytr.api import TradeRepublicApi

    if not is_configured():
        raise ValueError("TR_PHONE y TR_PIN no están configurados en .env")

    tr = TradeRepublicApi(phone_no=TR_PHONE, pin=TR_PIN, keyfile=TR_KEYFILE)
    tr.initiate_device_reset()

    _SETUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETUP_STATE_FILE.write_text(json.dumps({
        "process_id": tr._process_id,
        "sk_pem":     tr.sk.to_pem().decode("ascii"),
    }))

    return f"SMS enviado a {TR_PHONE}. Introduce el código para completar la vinculación."


def complete_setup(code: str) -> str:
    """
    Paso 2: confirma con el código SMS y guarda el keyfile.
    Retorna mensaje de éxito o lanza ValueError.
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
