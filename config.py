import os

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MODEL = os.getenv("MODEL", "claude-haiku-4-5-20251001")

DATABASE = "data/radar.db"
OUTPUT_DIR = "output"

REPORT_HOUR = int(os.getenv("REPORT_HOUR", "8"))
TIMEZONE = os.getenv("TIMEZONE", "UTC")
