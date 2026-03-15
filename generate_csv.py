import math
import os
from datetime import date
import pandas as pd
import yaml

from fetch_data import fetch_stock_data, to_eur, clear_fx_cache
from database import get_trend, get_portfolio_position, get_ticker_history
from config import OUTPUT_DIR

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

    market_cap = info.get("marketCap")
    currency = info.get("currency", "USD")
    _cap_eur = to_eur(market_cap, currency) if market_cap else None
    market_cap_eur = round(_cap_eur / 1e9, 1) if _cap_eur is not None else None
    return {
        "pe_ratio":       val(info.get("trailingPE")),
        "pb_ratio":       val(info.get("priceToBook")),
        "profit_margin":  pct(info.get("profitMargins")),
        "roe":            pct(info.get("returnOnEquity")),
        "debt_equity":    val(info.get("debtToEquity")),
        "revenue_growth": pct(info.get("revenueGrowth")),
        "market_cap_b":   market_cap_eur,
    }

def _detect_trend(ticker):
    history = get_trend(ticker, days=5)
    if len(history) < 2:
        return None
    newest_dd = history[0][1]
    oldest_dd = history[-1][1]
    if newest_dd is None or oldest_dd is None:
        return None
    return "empeorando" if newest_dd < oldest_dd else "mejorando"

def generate():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    clear_fx_cache()

    try:
        with open("tickers.yaml") as f:
            tickers = yaml.safe_load(f)
    except FileNotFoundError:
        return pd.DataFrame(), ["tickers.yaml no encontrado"]
    except yaml.YAMLError as e:
        return pd.DataFrame(), [f"tickers.yaml inválido: {e}"]

    today = date.today().isoformat()
    rows = []
    errors = []

    for category, assets in (tickers or {}).items():
        for ticker, meta in (assets or {}).items():
            try:
                hist, dividends, info = fetch_stock_data(ticker)
                if hist.empty:
                    # Distinguish delisted/suspended from never-seen tickers
                    if get_ticker_history(ticker, days=1):
                        errors.append(f"{ticker}: ⚠️ sin datos recientes (posible baja o suspensión)")
                    else:
                        errors.append(f"{ticker}: sin datos")
                    continue

                close = hist["Close"].dropna()
                if close.empty:
                    errors.append(f"{ticker}: serie de precios vacía")
                    continue

                currency = info.get("currency", "USD")
                price_orig = close.iloc[-1]
                price = to_eur(price_orig, currency)
                high_52w = to_eur(close.tail(252).max(), currency)
                if not high_52w or math.isnan(high_52w):
                    errors.append(f"{ticker}: precio máximo 52s es 0 o NaN, datos corruptos")
                    continue
                drawdown = (price / high_52w - 1) * 100

                base_3m_raw = close.iloc[-63] if len(close) >= 63 else None
                base_6m_raw = close.iloc[-126] if len(close) >= 126 else None
                base_3m_eur = to_eur(base_3m_raw, currency) if base_3m_raw is not None else None
                base_6m_eur = to_eur(base_6m_raw, currency) if base_6m_raw is not None else None
                momentum_3m = (price / base_3m_eur - 1) * 100 if base_3m_eur and base_3m_eur > 0 else None
                momentum_6m = (price / base_6m_eur - 1) * 100 if base_6m_eur and base_6m_eur > 0 else None

                daily_returns = close.pct_change().dropna()
                volatility = daily_returns.tail(252).std() * (252 ** 0.5) * 100 if not daily_returns.empty else None

                div_yield = _dividend_yield(dividends, price_orig)
                fundamentals = _extract_fundamentals(info)
                trend = _detect_trend(ticker)

                pnl = None
                if category == "portfolio":
                    position = get_portfolio_position(ticker)
                    if position:
                        shares, avg_price = position
                        if avg_price:
                            pnl = (price - avg_price) / avg_price * 100

                rows.append({
                    "category": category,
                    "ticker": ticker,
                    "name": meta.get("name", ticker),
                    "block": meta.get("block", "—"),
                    "region": meta.get("region", "—"),
                    "target_weight": meta.get("target_weight"),
                    "price": round(price, 2),
                    "drawdown_52w": round(drawdown, 2),
                    "momentum_3m": _safe_round(momentum_3m),
                    "momentum_6m": _safe_round(momentum_6m),
                    "volatility": _safe_round(volatility),
                    "dividend_yield": round(div_yield, 2),
                    "trend": trend,
                    "pnl": _safe_round(pnl),
                    **fundamentals,
                    "date": today,
                })

            except Exception as e:
                errors.append(f"{ticker}: {e}")

    df = pd.DataFrame(rows)
    if not df.empty:
        df.to_csv(f"{OUTPUT_DIR}/precios_global.csv", index=False, encoding="utf-8")

    return df, errors
