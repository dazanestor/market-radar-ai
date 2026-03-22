"""
conftest.py — Fixtures compartidos para la suite de tests.

El fixture `tmp_db` redirige database.DATABASE a un fichero SQLite
temporal e inicializa el esquema, de modo que cada test tiene su
propia base de datos limpia sin tocar data/radar.db.
"""
import sys
from unittest.mock import MagicMock

import pytest

# ── Stubs de dependencias externas no instaladas en el entorno de tests ───────
# Se inyectan en sys.modules ANTES de que cualquier módulo del proyecto se importe.
# Esto permite importar los módulos del proyecto sin que fallen por falta de
# librerías como yfinance, anthropic, APScheduler, etc.

_EXTERNAL_STUBS = [
    "yfinance",
    "anthropic",
    "anthropic.types",
    "apscheduler",
    "apscheduler.schedulers",
    "apscheduler.schedulers.blocking",
    "apscheduler.triggers",
    "apscheduler.triggers.cron",
    "apscheduler.triggers.interval",
    "pyotp",
    "bcrypt",
    "segno",
    "slowapi",
    "slowapi.util",
    "slowapi.errors",
    "uvicorn",
    "fastapi",
    "fastapi.responses",
    "fastapi.staticfiles",
    "fastapi.templating",
    "fastapi.middleware",
    "fastapi.middleware.sessions",
    "starlette",
    "starlette.middleware",
    "starlette.middleware.sessions",
    "jinja2",
    "multipart",
    "python_multipart",
    "matplotlib",
    "matplotlib.pyplot",
    "matplotlib.figure",
    "matplotlib.backends",
    "matplotlib.backends.backend_agg",
    "scipy",
    "scipy.optimize",
    "tenacity",
    "requests",
]

for _mod in _EXTERNAL_STUBS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


@pytest.fixture()
def tmp_db(monkeypatch, tmp_path):
    """
    Crea una BD SQLite temporal y parchea database.DATABASE para que
    todos los helpers de database.py usen ese fichero en lugar del real.
    """
    import database

    db_path = str(tmp_path / "test_radar.db")
    monkeypatch.setattr(database, "DATABASE", db_path)
    database.init_db()
    yield db_path
