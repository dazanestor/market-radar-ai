import math
import os
from datetime import date
import pandas as pd

from fetch_data import fetch_stock_data, to_eur, clear_fx_cache
from database import get_trend, get_portfolio_position, get_ticker_history, get_tickers_as_yaml_dict

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
    clear_fx_cache()

    try:
        tickers = get_tickers_as_yaml_dict()
    except Exception as e:
        return pd.DataFrame(), [f"Error leyendo tickers de BD: {e}"]

    today = date.today().isoformat()
    rows = []
    errors = []

    for category, assets in (tickers or {}).items():
        if category not in ("portfolio", "watchlist"):
            continue
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
                momentum_3m = (price / base_3m_eur - 1) * 100 if base_3m_eur and not math.isnan(base_3m_eur) and base_3m_eur > 0 else None
                momentum_6m = (price / base_6m_eur - 1) * 100 if base_6m_eur and not math.isnan(base_6m_eur) and base_6m_eur > 0 else None

                daily_returns = close.pct_change().dropna()
                volatility = daily_returns.tail(252).std() * (252 ** 0.5) * 100 if not daily_returns.empty else None

                rsi_val = _rsi(close)
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
                })

            except Exception as e:
                errors.append(f"{ticker}: {e}")

    df = pd.DataFrame(rows)
    return df, errors
