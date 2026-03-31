import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import pandas as pd

logger = logging.getLogger(__name__)

from fetch_data import fetch_stock_data, to_eur
from database import get_all_positions, get_tickers_as_yaml_dict, get_all_trends, get_all_recent_tickers, update_ticker_fields

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
    "United States": "USA", "Switzerland": "Europa", "Denmark": "Europa",
    "United Kingdom": "Europa", "France": "Europa", "Germany": "Europa",
    "Netherlands": "Europa", "Sweden": "Europa", "Spain": "Europa",
    "Italy": "Europa", "Belgium": "Europa", "Finland": "Europa",
    "Norway": "Europa", "Portugal": "Europa",
    "Australia": "Asia-Pacífico", "Japan": "Asia-Pacífico",
    "China": "Asia-Pacífico", "Hong Kong": "Asia-Pacífico",
    "South Korea": "Asia-Pacífico", "India": "Asia-Pacífico",
    "Canada": "América", "Brazil": "América", "Mexico": "América",
}

_MAX_WORKERS = int(os.environ.get("FETCH_WORKERS", "10"))

def _safe_round(v, n=2):
    return round(v, n) if v is not None and not math.isnan(v) else None

def _dividend_yield(dividends, price):
    if dividends.empty or not price or price < 0.01:
        return 0.0
    one_year_ago = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=1)
    if dividends.index.tz is None:
        dividends.index = dividends.index.tz_localize("UTC")
    else:
        dividends.index = dividends.index.tz_convert("UTC")
    annual = dividends[dividends.index >= one_year_ago].sum()
    return (annual / price) * 100

def _extract_fundamentals(info):
    def pct(v):
        return round(v * 100, 1) if v is not None and not math.isnan(v) else None
    def val(v, decimals=2):
        return round(v, decimals) if v is not None and not math.isnan(v) else None

    currency = info.get("currency", "USD")

    market_cap = info.get("marketCap")
    _cap_eur = to_eur(market_cap, currency) if market_cap else None
    market_cap_eur = round(_cap_eur / 1e9, 1) if _cap_eur is not None and not math.isnan(_cap_eur) else None

    # Consenso de analistas
    target_raw = info.get("targetMeanPrice")
    analyst_target_eur = None
    if target_raw is not None:
        try:
            if not math.isnan(float(target_raw)):
                converted = to_eur(float(target_raw), currency)
                if converted and not math.isnan(converted):
                    analyst_target_eur = round(converted, 2)
        except (TypeError, ValueError):
            pass

    return {
        "pe_ratio":       val(info.get("trailingPE")),
        "pb_ratio":       val(info.get("priceToBook")),
        "profit_margin":  pct(info.get("profitMargins")),
        "roe":            pct(info.get("returnOnEquity")),
        "debt_equity":    val(info.get("debtToEquity")),
        "revenue_growth": pct(info.get("revenueGrowth")),
        "market_cap_b":   market_cap_eur,
        "analyst_rec":    val(info.get("recommendationMean")),  # 1=Strong Buy … 5=Strong Sell
        "analyst_target": analyst_target_eur,                   # precio objetivo medio (EUR)
        "analyst_n":      info.get("numberOfAnalystOpinions"),  # nº de analistas
    }

def _rsi(close, period=14):
    """RSI de N períodos sobre la serie de precios de cierre."""
    delta = close.diff().dropna()
    if len(delta) < period:
        return None
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi_series = 100 - (100 / (1 + rs))
    last = rsi_series.dropna()
    return round(float(last.iloc[-1]), 1) if not last.empty else None


def _detect_trend(ticker, trends_cache: dict):
    history = trends_cache.get(ticker, [])
    if len(history) < 2:
        return None
    newest_dd = history[0][1]
    oldest_dd = history[-1][1]
    if newest_dd is None or oldest_dd is None:
        return None
    return "empeorando" if newest_dd < oldest_dd else "mejorando"


def _process_ticker(ticker, category, meta, today, portfolio_positions,
                    trends_cache: dict, recent_tickers: set):
    """Descarga y calcula métricas para un ticker. Retorna (row_dict | None, error_str | None)."""
    try:
        hist, dividends, info = fetch_stock_data(ticker)
        if hist.empty:
            # Distinguish delisted/suspended from never-seen tickers
            if ticker in recent_tickers:
                return None, f"{ticker}: ⚠️ sin datos recientes (posible baja o suspensión)"
            else:
                return None, f"{ticker}: sin datos"

        close = hist["Close"].dropna()
        if close.empty:
            return None, f"{ticker}: serie de precios vacía"

        currency = info.get("currency", "USD")
        price_orig = close.iloc[-1]
        price = to_eur(price_orig, currency)
        high_52w = to_eur(close.tail(252).max(), currency)
        if not high_52w or math.isnan(high_52w):
            return None, f"{ticker}: precio máximo 52s es 0 o NaN, datos corruptos"
        drawdown = (price / high_52w - 1) * 100

        base_3m_raw = close.iloc[-63] if len(close) >= 63 else None
        base_6m_raw = close.iloc[-126] if len(close) >= 126 else None
        base_3m_eur = to_eur(base_3m_raw, currency) if base_3m_raw is not None else None
        base_6m_eur = to_eur(base_6m_raw, currency) if base_6m_raw is not None else None
        momentum_3m = (price / base_3m_eur - 1) * 100 if base_3m_eur and not math.isnan(base_3m_eur) and base_3m_eur > 0 else None
        momentum_6m = (price / base_6m_eur - 1) * 100 if base_6m_eur and not math.isnan(base_6m_eur) and base_6m_eur > 0 else None

        daily_returns = close.pct_change().dropna()
        volatility = daily_returns.tail(252).std() * (252 ** 0.5) * 100 if len(daily_returns) >= 2 else None

        rsi_val = _rsi(close)
        div_yield = _dividend_yield(dividends, price_orig)
        fundamentals = _extract_fundamentals(info)
        trend = _detect_trend(ticker, trends_cache)

        pnl = None
        if category == "portfolio":
            position = portfolio_positions.get(ticker)
            if position:
                shares, avg_price = position
                if avg_price:
                    pnl = (price - avg_price) / avg_price * 100

        yf_name   = info.get("longName") or info.get("shortName")
        yf_sector = info.get("sector", "")
        yf_country = info.get("country", "")
        yf_block  = _SECTOR_TO_BLOCK.get(yf_sector, yf_sector or None)
        yf_region = _COUNTRY_TO_REGION.get(yf_country, yf_country or None)

        name   = meta.get("name")   or yf_name   or ticker
        block  = meta.get("block")  or yf_block  or None
        region = meta.get("region") or yf_region or None

        # Persistir en tickers si faltaban (enriquecimiento automático)
        updates = {}
        if not meta.get("name")   and yf_name:   updates["name"]   = yf_name
        if not meta.get("block")  and yf_block:  updates["block"]  = yf_block
        if not meta.get("region") and yf_region: updates["region"] = yf_region
        if updates:
            try:
                update_ticker_fields(ticker, **updates)
            except Exception:
                pass  # no crítico: solo enriquecimiento

        return {
            "category": category,
            "ticker": ticker,
            "name":   name,
            "block":  block,
            "region": region,
            "target_weight": meta.get("target_weight"),
            "target_price": meta.get("target_price"),
            "horizon": meta.get("horizon"),
            "price": round(price, 2),
            "drawdown_52w": round(drawdown, 2),
            "momentum_3m": _safe_round(momentum_3m),
            "momentum_6m": _safe_round(momentum_6m),
            "volatility": _safe_round(volatility),
            "dividend_yield": round(div_yield, 2),
            "rsi": rsi_val,
            "trend": trend,
            "pnl": _safe_round(pnl),
            **fundamentals,
            "date": today,
        }, None

    except Exception as e:
        logger.exception("Error procesando ticker %s", ticker)
        # ISO 27001 A.5 — no exponer detalles de excepción en mensajes de error
        return None, f"{ticker}: error al procesar datos"


def generate():
    try:
        tickers = get_tickers_as_yaml_dict()
    except Exception as e:
        logger.exception("Error leyendo tickers de BD")
        return pd.DataFrame(), ["Error leyendo tickers de BD"]

    today = date.today().isoformat()

    # Pre-fetch en una sola query cada fuente de datos de BD (evita N conexiones en paralelo)
    portfolio_positions = {r[0]: (r[1], r[2]) for r in get_all_positions()}
    trends_cache        = get_all_trends(days=5)
    recent_tickers      = get_all_recent_tickers(days=1)

    # Build flat task list
    tasks = []
    for category, assets in (tickers or {}).items():
        if category not in ("portfolio", "watchlist"):
            continue
        for ticker, meta in (assets or {}).items():
            tasks.append((ticker, category, meta))

    if not tasks:
        return pd.DataFrame(), []

    rows = []
    errors = []

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {
            pool.submit(_process_ticker, ticker, cat, meta, today,
                        portfolio_positions, trends_cache, recent_tickers): ticker
            for ticker, cat, meta in tasks
        }
        for fut in as_completed(futures):
            try:
                row, err = fut.result()
                if row:
                    rows.append(row)
                elif err:
                    errors.append(err)
            except Exception as e:
                ticker = futures[fut]
                logger.exception("Error inesperado procesando %s", ticker)
                errors.append(f"{ticker}: error inesperado al procesar")

    df = pd.DataFrame(rows)
    return df, errors
