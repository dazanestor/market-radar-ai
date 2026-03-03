import os

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MODEL = os.getenv("MODEL", "gpt-4o-mini")

DATABASE = "radar.db"
OUTPUT_DIR = "output"
