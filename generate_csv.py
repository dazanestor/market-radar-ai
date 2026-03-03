import pandas as pd
import yaml
from fetch_data import fetch_stock_data
from config import OUTPUT_DIR

def generate():
    with open("tickers.yaml") as f:
        tickers = yaml.safe_load(f)

    rows = []

    for category in tickers:
        for ticker, data in tickers[category].items():
            hist, dividends = fetch_stock_data(ticker)
            if hist.empty:
                continue

            price = hist["Close"].iloc[-1]
            high_52w = hist["Close"].tail(252).max()
            drawdown = (price / high_52w - 1) * 100

            rows.append({
                "ticker": ticker,
                "name": data["name"],
                "price": price,
                "drawdown_52w": drawdown,
                "block": data["block"],
                "region": data["region"]
            })

    df = pd.DataFrame(rows)
    df.to_csv(f"{OUTPUT_DIR}/precios_global.csv", index=False)
