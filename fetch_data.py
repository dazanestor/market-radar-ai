import anthropic
import yfinance as yf
from datetime import datetime, timezone

from config import ANTHROPIC_API_KEY, MODEL

_translate_cache: dict = {}


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
    Usa caché en memoria para no retradducir titulares ya vistos.
    Si la llamada falla, devuelve los titulares originales.
    """
    if not headlines:
        return headlines

    to_translate = [h for h in headlines if h not in _translate_cache]

    if to_translate:
        try:
            client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            numbered = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(to_translate))
            prompt   = (
                "Traduce al español solo el titular de cada noticia financiera. "
                "Conserva exactamente el símbolo • al inicio y el texto entre "
                "paréntesis (fuente y fecha) sin modificarlo. "
                "Responde únicamente con los titulares numerados, sin explicaciones.\n\n"
                + numbered
            )
            response = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            translated: list[str] = []
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
                _translate_cache[orig] = trans
            for h in to_translate[len(translated):]:
                _translate_cache[h] = h

        except Exception:
            for h in to_translate:
                _translate_cache[h] = h

    return [_translate_cache.get(h, h) for h in headlines]


def get_news(ticker, n=3, translate=False):
    """Devuelve los últimos n titulares del ticker.

    Si translate=True, los traduce al español con Claude antes de devolverlos.
    """
    try:
        items     = yf.Ticker(ticker).news or []
        headlines = []
        for item in items[:n]:
            title     = item.get("title") or item.get("content", {}).get("title", "")
            publisher = (item.get("publisher") or
                         item.get("content", {}).get("provider", {}).get("displayName", ""))
            ts        = item.get("providerPublishTime")
            if not title:
                continue
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b") if ts else ""
            headlines.append(f"• {title} ({publisher}{', ' + date_str if date_str else ''})")

        if translate and headlines:
            headlines = translate_headlines(headlines)
        return headlines
    except Exception:
        return []


def get_macro_context():
    """Obtiene S&P500, VIX y bono a 10 años para contexto macro."""
    try:
        result = {}

        spy_hist = yf.Ticker("SPY").history(period="1y")
        if not spy_hist.empty:
            price = spy_hist["Close"].iloc[-1]
            first = spy_hist["Close"].iloc[0]
            high  = spy_hist["Close"].tail(252).max()
            result["sp500_price"] = round(price, 2)
            if first:
                result["sp500_ytd"] = round((price / first - 1) * 100, 1)
            if high:
                result["sp500_drawdown"] = round((price / high - 1) * 100, 1)

        vix_hist = yf.Ticker("^VIX").history(period="5d")
        if not vix_hist.empty:
            result["vix"] = round(vix_hist["Close"].iloc[-1], 1)

        tnx_hist = yf.Ticker("^TNX").history(period="5d")
        if not tnx_hist.empty:
            result["treasury_10y"] = round(tnx_hist["Close"].iloc[-1], 2)

        return result
    except Exception:
        return {}
