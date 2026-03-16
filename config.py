import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Todas las variables son opcionales al arrancar.
# Pueden configurarse desde el dashboard web (se guardan en la BD SQLite).
# Prioridad en runtime: BD > variable de entorno > default.

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
MODEL              = os.getenv("MODEL", "claude-haiku-4-5-20251001")

TR_PHONE        = os.getenv("TR_PHONE", "")
TR_PIN          = os.getenv("TR_PIN",   "")
TR_COOKIES_FILE = os.getenv("TR_COOKIES_FILE", "data/tr_cookies.txt")

DATABASE   = "data/radar.db"
OUTPUT_DIR = "output"

# Constantes compartidas entre bot.py y scheduler.py
TELEGRAM_MAX_CHARS       = 4096
DRAWDOWN_ALERT_THRESHOLD = -20


def split_telegram_text(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list:
    """Divide un texto largo en bloques respetando saltos de línea."""
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

try:
    REPORT_HOUR = int(os.getenv("REPORT_HOUR", "8"))
    if not 0 <= REPORT_HOUR <= 23:
        REPORT_HOUR = 8
except ValueError:
    REPORT_HOUR = 8

TIMEZONE = os.getenv("TIMEZONE", "Europe/Madrid")
try:
    ZoneInfo(TIMEZONE)
except (ZoneInfoNotFoundError, KeyError):
    TIMEZONE = "Europe/Madrid"
