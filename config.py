import os
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Trade Republic (opcional)
TR_PHONE   = os.getenv("TR_PHONE", "")
TR_PIN     = os.getenv("TR_PIN", "")
TR_KEYFILE = os.getenv("TR_KEYFILE", "data/tr_keyfile.pem")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MODEL = os.getenv("MODEL", "claude-haiku-4-5-20251001")

DATABASE = "data/radar.db"
OUTPUT_DIR = "output"

try:
    REPORT_HOUR = int(os.getenv("REPORT_HOUR", "8"))
    if not 0 <= REPORT_HOUR <= 23:
        raise ValueError
except ValueError:
    print("ERROR: REPORT_HOUR debe ser un número entero entre 0 y 23.", file=sys.stderr)
    sys.exit(1)

TIMEZONE = os.getenv("TIMEZONE", "UTC")
try:
    ZoneInfo(TIMEZONE)
except (ZoneInfoNotFoundError, KeyError):
    print(f"ERROR: TIMEZONE inválido: '{TIMEZONE}'. Ejemplo válido: Europe/Madrid", file=sys.stderr)
    sys.exit(1)

_missing = [v for v in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(v)]
if _missing:
    print(f"ERROR: Variables de entorno requeridas no definidas: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)
