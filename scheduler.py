"""
Ejecución standalone sin bot de Telegram.
Uso: python scheduler.py
"""
from generate_csv import generate
from scoring import score_watchlist
from ai_analysis import analyze
from database import init_db, save_snapshot, save_report
from fetch_data import get_macro_context, get_news
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=15)
    except requests.RequestException as e:
        print(f"Error enviando mensaje a Telegram: {e}")

def run():
    print("Ejecutando radar de mercado...")
    macro = get_macro_context()
    df, errors = generate()

    if df.empty:
        send_telegram("❌ Error: no se pudo obtener datos de ningún ticker.")
        return

    df = score_watchlist(df)
    save_snapshot(df.to_dict("records"))

    portfolio_df = df[df["category"] == "portfolio"].copy()
    watchlist_df = df[df["category"] == "watchlist"].copy()

    news_by_ticker = {ticker: get_news(ticker) for ticker in df["ticker"].tolist()}

    try:
        ai_report = analyze(portfolio_df, watchlist_df, macro=macro, news_by_ticker=news_by_ticker)
    except Exception as e:
        print(f"Error al generar análisis con IA: {e}")
        send_telegram(f"❌ Error al generar análisis con IA: {e}")
        return

    save_report(ai_report)

    message = ai_report
    if errors:
        message += f"\n\n⚠️ *Tickers con error:* {', '.join(errors)}"

    send_telegram(message)
    print("Reporte enviado.")

if __name__ == "__main__":
    init_db()
    run()
