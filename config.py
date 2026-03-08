import os
import sys

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MODEL = os.getenv("MODEL", "claude-haiku-4-5-20251001")

DATABASE = "data/radar.db"
OUTPUT_DIR = "output"

try:
    REPORT_HOUR = int(os.getenv("REPORT_HOUR", "8"))
except ValueError:
    print("ERROR: REPORT_HOUR debe ser un número entero (0-23).", file=sys.stderr)
    sys.exit(1)

TIMEZONE = os.getenv("TIMEZONE", "UTC")
try:
    import pytz
    pytz.timezone(TIMEZONE)
except Exception:
    print(f"ERROR: TIMEZONE inválido: '{TIMEZONE}'. Ejemplo válido: Europe/Madrid", file=sys.stderr)
    sys.exit(1)

_missing = [v for v in ("ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(v)]
if _missing:
    print(f"ERROR: Variables de entorno requeridas no definidas: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)
