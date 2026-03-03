import pandas as pd
from generate_csv import generate
from scoring import score_watchlist
from ai_analysis import analyze
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})

def run():
    generate()

    df = pd.read_csv("output/precios_global.csv")
    df = score_watchlist(df)

    summary = df.to_string()
    ai_report = analyze(summary)

    send_telegram(ai_report)

if __name__ == "__main__":
    run()
