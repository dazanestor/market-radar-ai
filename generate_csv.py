import os
from datetime import date
import pandas as pd
import yaml

from fetch_data import fetch_stock_data
from database import get_trend, get_portfolio_position
from config import OUTPUT_DIR

def _safe_round(v, n=2):
    return round(v, n) if v is not None else None

def _dividend_yield(dividends, price):
    if dividends.empty or price == 0:
        return 0.0
    one_year_ago = pd.Timestamp.now(tz="UTC") - pd.DateOffset(years=1)
    if dividends.index.tz is None:
        dividends.index = dividends.index.tz_localize("UTC")
    annual = dividends[dividends.index >= one_year_ago].sum()
    return (annual / price) * 100

def _extract_fundamentals(info):
    def pct(v):
        return round(v * 100, 1) if v is not None else None
    def val(v, decimals=2):
        return round(v, decimals) if v is not None else None

    market_cap = info.get("marketCap")
    return {
        "pe_ratio":       val(info.get("trailingPE")),
        "pb_ratio":       val(info.get("priceToBook")),
        "profit_margin":  pct(info.get("profitMargins")),
        "roe":            pct(info.get("returnOnEquity")),
        "debt_equity":    val(info.get("debtToEquity")),
        "revenue_growth": pct(info.get("revenueGrowth")),
        "market_cap_b":   round(market_cap / 1e9, 1) if market_cap else None,
    }

def _detect_trend(ticker):
    history = get_trend(ticker, days=5)
    if len(history) < 2:
        return None
    newest_dd = history[0][1]
    oldest_dd = history[-1][1]
    return "empeorando" if newest_dd < oldest_dd else "mejorando"

def generate():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open("tickers.yaml") as f:
        tickers = yaml.safe_load(f)

    today = date.today().isoformat()
    rows = []
    errors = []

    for category, assets in tickers.items():
        for ticker, meta in assets.items():
            try:
                hist, dividends, info = fetch_stock_data(ticker)
                if hist.empty:
                    errors.append(f"{ticker}: sin datos")
                    continue

                close = hist["Close"].dropna()
                if close.empty:
                    errors.append(f"{ticker}: serie de precios vacía")
                    continue

                price = close.iloc[-1]
                high_52w = close.tail(252).max()
                drawdown = (price / high_52w - 1) * 100

                momentum_3m = (price / close.iloc[-63] - 1) * 100 if len(close) >= 63 else None
                momentum_6m = (price / close.iloc[-126] - 1) * 100 if len(close) >= 126 else None

                daily_returns = close.pct_change().dropna()
                volatility = daily_returns.tail(252).std() * (252 ** 0.5) * 100 if not daily_returns.empty else None

                div_yield = _dividend_yield(dividends, price)
                fundamentals = _extract_fundamentals(info)
                trend = _detect_trend(ticker)

                pnl = None
                if category == "portfolio":
                    position = get_portfolio_position(ticker)
                    if position:
                        shares, avg_price = position
                        pnl = (price - avg_price) / avg_price * 100

                rows.append({
                    "category": category,
                    "ticker": ticker,
                    "name": meta["name"],
                    "block": meta["block"],
                    "region": meta["region"],
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
        df.to_csv(f"{OUTPUT_DIR}/precios_global.csv", index=False)

    return df, errors
