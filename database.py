import hashlib
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime
from typing import Optional
from config import DATABASE

logger = logging.getLogger("database")

_vacuum_lock = threading.Lock()


def init_db():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS portfolio (
            ticker TEXT PRIMARY KEY,
            shares REAL,
            avg_price REAL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            date TEXT,
            price REAL,
            drawdown_52w REAL,
            momentum_3m REAL,
            momentum_6m REAL,
            volatility REAL,
            dividend_yield REAL,
            rsi REAL,
            score REAL,
            opportunity TEXT,
            UNIQUE(ticker, date)
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS price_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            target_price REAL,
            direction TEXT,
            condition_type TEXT DEFAULT 'price',
            condition_value REAL,
            active INTEGER DEFAULT 1,
            created TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            target_price REAL,
            direction TEXT,
            condition_type TEXT DEFAULT 'price',
            condition_value REAL,
            triggered_at TEXT,
            price_at_trigger REAL,
            notified INTEGER DEFAULT 0
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            content TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS tr_cache (
            key     TEXT PRIMARY KEY,
            value   TEXT,
            updated TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS news_cache (
            headline_hash TEXT PRIMARY KEY,
            translation   TEXT,
            fetched_at    TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            type TEXT NOT NULL,
            shares REAL NOT NULL,
            price_eur REAL NOT NULL,
            notes TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_value (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            total_eur REAL NOT NULL,
            positions_count INTEGER
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL,
            updated TEXT
        )
        """)
        # Migrations: add new columns before creating indexes that depend on them
        _add_column_if_missing(conn, "price_alerts",  "condition_type",  "TEXT DEFAULT 'price'")
        _add_column_if_missing(conn, "price_alerts",  "condition_value", "REAL")
        _add_column_if_missing(conn, "alert_history", "notified",        "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "price_history", "rsi",             "REAL")

        c.execute("CREATE INDEX IF NOT EXISTS idx_price_history_ticker ON price_history(ticker)")
        # Composite index for get_ticker_history queries (ticker + date DESC)
        c.execute("CREATE INDEX IF NOT EXISTS idx_price_history_ticker_date ON price_history(ticker, date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(active)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alert_history_triggered ON alert_history(triggered_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alert_history_notified ON alert_history(notified)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_operations_ticker ON operations(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_operations_date ON operations(date DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_value_date ON portfolio_value(date DESC)")


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    except sqlite3.OperationalError:
        pass  # Column already exists


@contextmanager
def _db():
    conn = sqlite3.connect(DATABASE)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── price_history ─────────────────────────────────────────────────────────────

def save_snapshot(rows):
    with _db() as conn:
        c = conn.cursor()
        for row in rows:
            c.execute("""
            INSERT OR REPLACE INTO price_history
                (ticker, date, price, drawdown_52w, momentum_3m, momentum_6m,
                 volatility, dividend_yield, rsi, score, opportunity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["ticker"], row["date"], row["price"], row["drawdown_52w"],
                row.get("momentum_3m"), row.get("momentum_6m"),
                row.get("volatility"), row.get("dividend_yield"),
                row.get("rsi"),
                row.get("score"), row.get("opportunity")
            ))

def get_trend(ticker, days=5):
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT date, drawdown_52w FROM price_history
            WHERE ticker = ?
            ORDER BY date DESC
            LIMIT ?
        """, (ticker, days))
        return c.fetchall()

def get_ticker_history(ticker, days=30):
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT date, price, drawdown_52w, score, opportunity
            FROM price_history
            WHERE ticker = ?
            ORDER BY date DESC
            LIMIT ?
        """, (ticker, days))
        return c.fetchall()


# ── portfolio positions ───────────────────────────────────────────────────────

def get_portfolio_position(ticker):
    with _db() as conn:
        c = conn.cursor()
        c.execute("SELECT shares, avg_price FROM portfolio WHERE ticker = ?", (ticker,))
        return c.fetchone()

def upsert_position(ticker, shares, avg_price):
    with _db() as conn:
        conn.cursor().execute("""
            INSERT INTO portfolio (ticker, shares, avg_price)
            VALUES (?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET shares=excluded.shares, avg_price=excluded.avg_price
        """, (ticker, shares, avg_price))

def delete_position(ticker):
    with _db() as conn:
        conn.cursor().execute("DELETE FROM portfolio WHERE ticker = ?", (ticker,))

def get_all_positions():
    with _db() as conn:
        c = conn.cursor()
        c.execute("SELECT ticker, shares, avg_price FROM portfolio")
        return c.fetchall()


# ── price alerts ──────────────────────────────────────────────────────────────

def add_price_alert(ticker, target_price, direction,
                    condition_type: str = "price", condition_value: Optional[float] = None):
    with _db() as conn:
        conn.cursor().execute("""
            INSERT INTO price_alerts
                (ticker, target_price, direction, condition_type, condition_value, active, created)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (ticker, target_price, direction, condition_type, condition_value,
              date.today().isoformat()))

def get_active_alerts():
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, ticker, target_price, direction, created,
                   condition_type, condition_value
            FROM price_alerts WHERE active = 1
        """)
        return c.fetchall()

def deactivate_alert(alert_id):
    with _db() as conn:
        conn.cursor().execute(
            "UPDATE price_alerts SET active = 0 WHERE id = ?", (alert_id,)
        )


# ── alert_history ─────────────────────────────────────────────────────────────

def log_alert_triggered(ticker: str, target_price: float, direction: str,
                        price_at_trigger: float,
                        condition_type: str = "price",
                        condition_value: Optional[float] = None) -> int:
    """Registra alerta disparada con notified=0. Devuelve el ID insertado."""
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO alert_history
                (ticker, target_price, direction, condition_type, condition_value,
                 triggered_at, price_at_trigger, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (ticker, target_price, direction, condition_type, condition_value,
              datetime.now().isoformat(timespec="minutes"), price_at_trigger))
        return cur.lastrowid


def mark_alert_notified(history_id: int) -> None:
    with _db() as conn:
        conn.cursor().execute(
            "UPDATE alert_history SET notified = 1 WHERE id = ?", (history_id,)
        )


def get_unnotified_alerts(limit: int = 20):
    """Devuelve alertas guardadas pero no notificadas (ej.: el bot estaba caído)."""
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, ticker, target_price, direction, condition_type,
                   condition_value, triggered_at, price_at_trigger
            FROM alert_history WHERE notified = 0
            ORDER BY id ASC LIMIT ?
        """, (limit,))
        return c.fetchall()


def get_alert_history(limit: int = 50):
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, ticker, target_price, direction, condition_type,
                   condition_value, triggered_at, price_at_trigger
            FROM alert_history
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        return c.fetchall()


# ── tr_cache ──────────────────────────────────────────────────────────────────

def set_tr_cache(key: str, value: str):
    with _db() as conn:
        conn.cursor().execute("""
            INSERT INTO tr_cache (key, value, updated)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated
        """, (key, value, datetime.now().isoformat(timespec="minutes")))


def get_tr_cache(key: str):
    with _db() as conn:
        c = conn.cursor()
        c.execute("SELECT value, updated FROM tr_cache WHERE key = ?", (key,))
        return c.fetchone()


# ── reports ───────────────────────────────────────────────────────────────────

def save_report(content):
    with _db() as conn:
        conn.cursor().execute(
            "INSERT INTO reports (date, content) VALUES (?, ?)",
            (datetime.now().isoformat(timespec="minutes"), content)
        )

def get_recent_reports(n: int = 5, offset: int = 0):
    with _db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, date, content FROM reports ORDER BY id DESC LIMIT ? OFFSET ?",
            (n, offset)
        )
        return c.fetchall()

def count_reports() -> int:
    with _db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM reports")
        row = c.fetchone()
        return row[0] if row else 0


# ── news_cache ────────────────────────────────────────────────────────────────

def _headline_hash(headline: str) -> str:
    return hashlib.sha256(headline.encode("utf-8")).hexdigest()[:32]

def cache_news_translation(headline: str, translation: str) -> None:
    h = _headline_hash(headline)
    with _db() as conn:
        conn.cursor().execute("""
            INSERT INTO news_cache (headline_hash, translation, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(headline_hash) DO UPDATE
                SET translation=excluded.translation, fetched_at=excluded.fetched_at
        """, (h, translation, datetime.now().isoformat(timespec="minutes")))

def purge_old_price_history(days: int = 365) -> int:
    """Elimina snapshots de price_history con más de `days` días. Devuelve filas eliminadas."""
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM price_history WHERE date < date('now', ?)",
            (f"-{days} days",)
        )
        return cur.rowcount


def purge_old_news_cache(days: int = 30) -> int:
    """Elimina traducciones de news_cache con más de `days` días. Devuelve filas eliminadas."""
    with _db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM news_cache WHERE fetched_at < datetime('now', ?)",
            (f"-{days} days",)
        )
        return cur.rowcount


def vacuum_db() -> None:
    """Ejecuta VACUUM + WAL checkpoint para reducir tamaño del fichero SQLite."""
    with _vacuum_lock:
        conn = sqlite3.connect(DATABASE)
        try:
            conn.isolation_level = None  # autocommit requerido para VACUUM
            conn.execute("VACUUM")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            logger.info("VACUUM completado.")
        finally:
            conn.close()


# ── operations (historial de operaciones) ──────────────────────────────────────

def add_operation(ticker: str, date: str, op_type: str, shares: float, price_eur: float, notes: str = "") -> int:
    """Registra una operación de compra/venta. Devuelve el ID insertado."""
    with _db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO operations (ticker, date, type, shares, price_eur, notes)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (ticker, date, op_type, shares, price_eur, notes or ""))
        return cur.lastrowid


def delete_operation(op_id: int) -> None:
    with _db() as conn:
        conn.cursor().execute("DELETE FROM operations WHERE id = ?", (op_id,))


def get_operations(ticker: str = None, limit: int = 200) -> list:
    """Devuelve operaciones ordenadas por fecha DESC. Filtra por ticker si se especifica."""
    with _db() as conn:
        c = conn.cursor()
        if ticker:
            c.execute("""
                SELECT id, ticker, date, type, shares, price_eur, notes
                FROM operations WHERE ticker = ?
                ORDER BY date DESC, id DESC LIMIT ?
            """, (ticker, limit))
        else:
            c.execute("""
                SELECT id, ticker, date, type, shares, price_eur, notes
                FROM operations ORDER BY date DESC, id DESC LIMIT ?
            """, (limit,))
        return c.fetchall()


def count_operations() -> int:
    with _db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM operations")
        row = c.fetchone()
        return row[0] if row else 0


# ── portfolio_value (evolución del valor) ──────────────────────────────────────

def save_portfolio_value(total_eur: float, positions_count: int = 0) -> None:
    """Guarda (o actualiza) el valor total de la cartera para hoy."""
    today = date.today().isoformat()
    with _db() as conn:
        conn.cursor().execute("""
            INSERT INTO portfolio_value (date, total_eur, positions_count)
            VALUES (?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET total_eur=excluded.total_eur,
                positions_count=excluded.positions_count
        """, (today, total_eur, positions_count))


def get_portfolio_value_history(days: int = 365) -> list:
    """Devuelve historial de valor total ordenado por fecha ASC."""
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT date, total_eur, positions_count
            FROM portfolio_value
            ORDER BY date ASC
            LIMIT ?
        """, (days,))
        return c.fetchall()


# ── settings ──────────────────────────────────────────────────────────────────

def get_setting(key: str) -> Optional[str]:
    """Devuelve el valor guardado en BD para la clave, o None si no existe."""
    with _db() as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = c.fetchone()
        return row[0] if row else None


def set_setting(key: str, value: str) -> None:
    with _db() as conn:
        conn.cursor().execute("""
            INSERT INTO settings (key, value, updated) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated
        """, (key, value, datetime.now().isoformat(timespec="minutes")))


def delete_setting(key: str) -> None:
    with _db() as conn:
        conn.cursor().execute("DELETE FROM settings WHERE key = ?", (key,))


def get_all_settings() -> dict:
    """Devuelve todos los settings guardados en BD como {key: value}."""
    with _db() as conn:
        c = conn.cursor()
        c.execute("SELECT key, value FROM settings")
        return dict(c.fetchall())


def get_cached_translation(headline: str, max_age_hours: int = 24) -> Optional[str]:
    h = _headline_hash(headline)
    with _db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT translation, fetched_at FROM news_cache WHERE headline_hash = ?", (h,)
        )
        row = c.fetchone()
    if not row:
        return None
    try:
        fetched = datetime.fromisoformat(row[1])
        age_hours = (datetime.now() - fetched).total_seconds() / 3600
        if age_hours > max_age_hours:
            return None
    except Exception:
        return None
    return row[0]
