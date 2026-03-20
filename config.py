import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Todas las variables son opcionales al arrancar.
# Pueden configurarse desde el dashboard web (se guardan en la BD SQLite).
# Prioridad en runtime: BD > variable de entorno > default.

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "")
MODEL              = os.getenv("MODEL", "claude-haiku-4-5-20251001")

TR_PHONE        = os.getenv("TR_PHONE", "")
TR_PIN          = os.getenv("TR_PIN",   "")
TR_COOKIES_FILE = os.getenv("TR_COOKIES_FILE", "data/tr_cookies.txt")

DATABASE   = "data/radar.db"
OUTPUT_DIR = "output"

DRAWDOWN_ALERT_THRESHOLD = -20

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
