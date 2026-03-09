import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from config import DATABASE


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
            active INTEGER DEFAULT 1,
            created TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            content TEXT
        )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_price_history_ticker ON price_history(ticker)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(active)")


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
                 volatility, dividend_yield, score, opportunity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["ticker"], row["date"], row["price"], row["drawdown_52w"],
                row.get("momentum_3m"), row.get("momentum_6m"),
                row.get("volatility"), row.get("dividend_yield"),
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

def add_price_alert(ticker, target_price, direction):
    with _db() as conn:
        conn.cursor().execute("""
            INSERT INTO price_alerts (ticker, target_price, direction, active, created)
            VALUES (?, ?, ?, 1, ?)
        """, (ticker, target_price, direction, date.today().isoformat()))

def get_active_alerts():
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT id, ticker, target_price, direction, created
            FROM price_alerts WHERE active = 1
        """)
        return c.fetchall()

def deactivate_alert(alert_id):
    with _db() as conn:
        conn.cursor().execute(
            "UPDATE price_alerts SET active = 0 WHERE id = ?", (alert_id,)
        )


# ── reports ───────────────────────────────────────────────────────────────────

def save_report(content):
    with _db() as conn:
        conn.cursor().execute(
            "INSERT INTO reports (date, content) VALUES (?, ?)",
            (datetime.now().isoformat(timespec="minutes"), content)
        )

def get_recent_reports(n=5):
    with _db() as conn:
        c = conn.cursor()
        c.execute("SELECT id, date, content FROM reports ORDER BY id DESC LIMIT ?", (n,))
        return c.fetchall()
