"""
Ejecución standalone sin bot de Telegram.
Uso: python scheduler.py
"""
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from generate_csv import generate
from scoring import score_watchlist
from ai_analysis import analyze
from database import init_db, save_snapshot, save_report, vacuum_db, effective
from fetch_data import get_macro_context, get_news
from config import TELEGRAM_MAX_CHARS, DRAWDOWN_ALERT_THRESHOLD

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)

def _split_text(text, limit=TELEGRAM_MAX_CHARS):
    chunks = []
    while len(text) > limit:
        split_at = text.rfind('\n', 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    if text:
        chunks.append(text)
    return chunks


def send_telegram(text):
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    token   = effective("TELEGRAM_BOT_TOKEN",   env_fallback=TELEGRAM_BOT_TOKEN)
    chat_id = effective("TELEGRAM_CHAT_ID",      env_fallback=TELEGRAM_CHAT_ID)
    if not token or not chat_id:
        logging.warning("send_telegram: credenciales Telegram no configuradas, omitiendo envío.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in _split_text(text):
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}, timeout=15)
            if not resp.json().get("ok"):
                try:
                    requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=15)
                except requests.RequestException as e:
                    logging.error(f"Error en reintento sin Markdown: {e}")
        except requests.RequestException as e:
            logging.error(f"Error enviando mensaje a Telegram: {e}")

def run():
    logging.info("Ejecutando radar de mercado...")
    macro = get_macro_context()
    df, errors = generate()

    if df.empty:
        send_telegram("❌ Error: no se pudo obtener datos de ningún ticker.")
        return

    df = score_watchlist(df)
    save_snapshot(df.to_dict("records"))

    portfolio_df = df[df["category"] == "portfolio"].copy()
    watchlist_df = df[df["category"] == "watchlist"].copy()

    ticker_list = df["ticker"].tolist()
    with ThreadPoolExecutor(max_workers=8) as pool:
        news_futures = {pool.submit(get_news, t): t for t in ticker_list}
        news_by_ticker = {}
        for fut in as_completed(news_futures, timeout=120):
            t = news_futures[fut]
            try:
                news_by_ticker[t] = fut.result()
            except Exception:
                news_by_ticker[t] = []

    try:
        ai_report = analyze(portfolio_df, watchlist_df, macro=macro, news_by_ticker=news_by_ticker)
    except Exception as e:
        logging.error(f"Error al generar análisis con IA: {e}")
        send_telegram(f"❌ Error al generar análisis con IA: {e}")
        return

    save_report(ai_report)

    drawdown_alerts = []
    for _, row in df.iterrows():
        if row["drawdown_52w"] < DRAWDOWN_ALERT_THRESHOLD:
            drawdown_alerts.append(
                f"⚠️ *{row['ticker']}* ({row['name']}): drawdown de {row['drawdown_52w']:.1f}% desde máximo anual"
            )
        if row.get("trend") == "empeorando":
            drawdown_alerts.append(f"📉 *{row['ticker']}*: drawdown en tendencia creciente esta semana")

    message = ai_report
    if drawdown_alerts:
        message = "\n".join(drawdown_alerts) + "\n\n" + message
    if errors:
        message += f"\n\n⚠️ *Tickers con error:* {', '.join(errors)}"

    send_telegram(message)
    logging.info("Reporte enviado.")

if __name__ == "__main__":
    init_db()
    run()
