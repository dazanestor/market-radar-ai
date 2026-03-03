import sqlite3
from config import DATABASE

def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS portfolio (
        ticker TEXT PRIMARY KEY,
        shares REAL,
        avg_price REAL
    )
    """)

    conn.commit()
    conn.close()
