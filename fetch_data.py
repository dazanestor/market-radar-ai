import yfinance as yf
from datetime import datetime, timezone

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

def get_news(ticker, n=3):
    """Devuelve los últimos n titulares del ticker."""
    try:
        items = yf.Ticker(ticker).news or []
        headlines = []
        for item in items[:n]:
            title = item.get("title") or item.get("content", {}).get("title", "")
            publisher = item.get("publisher") or item.get("content", {}).get("provider", {}).get("displayName", "")
            ts = item.get("providerPublishTime")
            if not title:
                continue
            date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b") if ts else ""
            headlines.append(f"• {title} ({publisher}{', ' + date_str if date_str else ''})")
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
            result["sp500_price"] = round(price, 2)
            result["sp500_ytd"] = round((price / spy_hist["Close"].iloc[0] - 1) * 100, 1)
            result["sp500_drawdown"] = round((price / spy_hist["Close"].tail(252).max() - 1) * 100, 1)

        vix_hist = yf.Ticker("^VIX").history(period="5d")
        if not vix_hist.empty:
            result["vix"] = round(vix_hist["Close"].iloc[-1], 1)

        tnx_hist = yf.Ticker("^TNX").history(period="5d")
        if not tnx_hist.empty:
            result["treasury_10y"] = round(tnx_hist["Close"].iloc[-1], 2)

        return result
    except Exception:
        return {}
