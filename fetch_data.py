import anthropic
import logging
import math
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from tenacity import retry, stop_after_attempt, wait_exponential

from config import ANTHROPIC_API_KEY, MODEL

logger = logging.getLogger("fetch_data")

_fx_cache: dict = {}        # {currency: (rate, timestamp)}
_FX_TTL = 3600              # 1 hora

# Caché de traducciones con TTL de 24h: {headline: (translation, timestamp)}
_TRANSLATE_TTL = 86400
_translate_cache: dict = {}


def _translate_cache_get(key: str):
    entry = _translate_cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    import time as _time
    if _time.monotonic() - ts > _TRANSLATE_TTL:
        del _translate_cache[key]
        return None
    return value


def _translate_cache_set(key: str, value: str):
    import time as _time
    _translate_cache[key] = (value, _time.monotonic())

def to_eur(price, currency):
    """Convierte un precio a EUR usando el tipo de cambio de yfinance."""
    import time as _time
    if price is None or (isinstance(price, float) and math.isnan(price)):
        return None
    if currency == "EUR" or not currency:
        return price
    # GBp = peniques británicos (yfinance los devuelve para .L) → convertir a GBP
    if currency == "GBp":
        price    = price / 100
        currency = "GBP"
    cached = _fx_cache.get(currency)
    if cached is None or (_time.monotonic() - cached[1]) > _FX_TTL:
        try:
            hist = yf.Ticker(f"{currency}EUR=X").history(period="2d")
            rate = float(hist["Close"].iloc[-1]) if not hist.empty else 1.0
            _fx_cache[currency] = (rate if not math.isnan(rate) else 1.0, _time.monotonic())
        except Exception:
            logging.warning(f"No se pudo obtener tipo de cambio {currency}EUR=X, usando 1.0 como fallback")
            _fx_cache[currency] = (1.0, _time.monotonic())
    return price * _fx_cache[currency][0]

def clear_fx_cache():
    _fx_cache.clear()

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20),
       reraise=True)
def fetch_stock_data(ticker):
    stock = yf.Ticker(ticker)
    hist = stock.history(period="5y")
    dividends = stock.dividends
    info = {}
    try:
        info = stock.info or {}
    except Exception:
        pass
    return hist, dividends, info


def translate_headlines(headlines: list) -> list:
    """Traduce una lista de titulares al español en una sola llamada a Claude.

    Conserva el símbolo •, el nombre del medio y la fecha entre paréntesis.
    Usa caché en BD (24h) y en memoria para no retradducir titulares ya vistos.
    Si la llamada falla, devuelve los titulares originales.
    """
    if not headlines:
        return headlines

    # Intentar cargar caché de BD
    try:
        from database import get_cached_translation, cache_news_translation
        _db_cache_available = True
    except Exception:
        _db_cache_available = False

    to_translate = []
    for h in headlines:
        if _translate_cache_get(h) is not None:
            continue
        if _db_cache_available:
            cached = get_cached_translation(h)
            if cached is not None:
                _translate_cache_set(h, cached)
                continue
        to_translate.append(h)

    if to_translate:
        try:
            try:
                from database import get_setting as _gs
                _api_key = _gs("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY
                _model   = _gs("MODEL") or MODEL
            except Exception:
                _api_key, _model = ANTHROPIC_API_KEY, MODEL
            client   = anthropic.Anthropic(api_key=_api_key)
            numbered = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(to_translate))
            prompt   = (
                "Traduce al español solo el titular de cada noticia financiera. "
                "Conserva exactamente el símbolo • al inicio y el texto entre "
                "paréntesis (fuente y fecha) sin modificarlo. "
                "Responde únicamente con los titulares numerados, sin explicaciones.\n\n"
                + numbered
            )
            response = client.messages.create(
                model=_model,
                max_tokens=1024,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            translated: list[str] = []
            if not response.content:
                raise ValueError("Respuesta vacía de Claude")
            for line in response.content[0].text.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Eliminar prefijo numérico "1. "
                if line and line[0].isdigit() and ". " in line:
                    line = line.split(". ", 1)[1]
                translated.append(line)

            # Guardar en caché; si Claude devuelve menos líneas, usar original
            for orig, trans in zip(to_translate, translated):
                _translate_cache_set(orig, trans)
                if _db_cache_available:
                    try:
                        cache_news_translation(orig, trans)
                    except Exception:
                        pass
            for h in to_translate[len(translated):]:
                _translate_cache_set(h, h)

        except Exception:
            logger.exception("Error traduciendo titulares con Claude")
            for h in to_translate:
                _translate_cache_set(h, h)

    return [_translate_cache_get(h) or h for h in headlines]


def get_news(ticker, n=3, translate=False):
    """Devuelve los últimos n titulares del ticker.

    Si translate=True, los traduce al español con Claude antes de devolverlos.
    """
    try:
        items     = yf.Ticker(ticker).news or []
        headlines = []
        for item in items[:n]:
            content   = item.get("content") if isinstance(item.get("content"), dict) else {}
            provider  = content.get("provider") if isinstance(content.get("provider"), dict) else {}
            title     = item.get("title") or content.get("title", "")
            publisher = item.get("publisher") or provider.get("displayName", "")
            ts        = item.get("providerPublishTime")
            if not title:
                continue
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b") if ts else ""
            meta_parts = [p for p in [publisher, date_str] if p]
            suffix = f" ({', '.join(meta_parts)})" if meta_parts else ""
            headlines.append(f"• {title}{suffix}")

        if translate and headlines:
            headlines = translate_headlines(headlines)
        return headlines
    except Exception:
        return []


def get_macro_context():
    """Obtiene S&P500, VIX y bono a 10 años para contexto macro (en paralelo)."""
    result = {}

    def _fetch_spy():
        try:
            hist = yf.Ticker("SPY").history(period="1y")
            if not hist.empty:
                price_usd = hist["Close"].iloc[-1]
                price_eur = to_eur(price_usd, "USD")
                out = {"sp500_price": round(price_eur, 2)}
                current_year = hist.index[-1].year
                ytd_data = hist[hist.index.year == current_year]
                if not ytd_data.empty:
                    out["sp500_ytd"] = round((price_usd / ytd_data["Close"].iloc[0] - 1) * 100, 1)
                out["sp500_drawdown"] = round((price_usd / hist["Close"].tail(252).max() - 1) * 100, 1)
                return out
        except Exception:
            pass
        return {}

    def _fetch_vix():
        try:
            hist = yf.Ticker("^VIX").history(period="5d")
            if not hist.empty:
                return {"vix": round(hist["Close"].iloc[-1], 1)}
        except Exception:
            pass
        return {}

    def _fetch_tnx():
        try:
            hist = yf.Ticker("^TNX").history(period="5d")
            if not hist.empty:
                return {"treasury_10y": round(hist["Close"].iloc[-1], 2)}
        except Exception:
            pass
        return {}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(f) for f in (_fetch_spy, _fetch_vix, _fetch_tnx)]
        for fut in futs:
            result.update(fut.result())

    return result
