"""
test_database.py — Tests de integración para database.py.

Cada test usa el fixture `tmp_db` (conftest.py) que redirige
database.DATABASE a un SQLite temporal limpio.
"""
import math
from datetime import date, datetime, timedelta

import pytest

import database


# ── settings ──────────────────────────────────────────────────────────────────

class TestSettings:
    def test_get_missing_key_returns_none(self, tmp_db):
        assert database.get_setting("clave_inexistente") is None

    def test_set_and_get(self, tmp_db):
        database.set_setting("api_key", "sk-test-123")
        assert database.get_setting("api_key") == "sk-test-123"

    def test_update_overwrites(self, tmp_db):
        database.set_setting("x", "v1")
        database.set_setting("x", "v2")
        assert database.get_setting("x") == "v2"

    def test_delete_removes_key(self, tmp_db):
        database.set_setting("borrar", "valor")
        database.delete_setting("borrar")
        assert database.get_setting("borrar") is None

    def test_get_all_settings(self, tmp_db):
        database.set_setting("k1", "a")
        database.set_setting("k2", "b")
        s = database.get_all_settings()
        assert s["k1"] == "a"
        assert s["k2"] == "b"


class TestEffective:
    def test_bd_overrides_env(self, tmp_db, monkeypatch):
        monkeypatch.setenv("MY_KEY", "from_env")
        database.set_setting("MY_KEY", "from_bd")
        import os
        result = database.effective("MY_KEY", env_fallback=os.getenv("MY_KEY", ""))
        assert result == "from_bd"

    def test_env_fallback_when_no_bd(self, tmp_db):
        result = database.effective("KEY_MISSING", env_fallback="env_value")
        assert result == "env_value"

    def test_default_when_nothing(self, tmp_db):
        result = database.effective("KEY_MISSING", env_fallback="", default="mi_default")
        assert result == "mi_default"


# ── portfolio positions ────────────────────────────────────────────────────────

class TestPortfolioPositions:
    def test_upsert_and_get(self, tmp_db):
        database.upsert_position("AAPL", 10, 150.0)
        row = database.get_portfolio_position("AAPL")
        assert row == (10.0, 150.0)

    def test_update_existing(self, tmp_db):
        database.upsert_position("MSFT", 5, 200.0)
        database.upsert_position("MSFT", 8, 210.0)
        row = database.get_portfolio_position("MSFT")
        assert row == (8.0, 210.0)

    def test_get_missing_returns_none(self, tmp_db):
        assert database.get_portfolio_position("NONEXISTENT") is None

    def test_delete_position(self, tmp_db):
        database.upsert_position("GOOG", 3, 120.0)
        database.delete_position("GOOG")
        assert database.get_portfolio_position("GOOG") is None

    def test_get_all_positions(self, tmp_db):
        database.upsert_position("A", 1, 10.0)
        database.upsert_position("B", 2, 20.0)
        positions = database.get_all_positions()
        tickers = [p[0] for p in positions]
        assert "A" in tickers
        assert "B" in tickers


# ── tickers ────────────────────────────────────────────────────────────────────

class TestTickers:
    def test_upsert_and_meta(self, tmp_db):
        database.upsert_ticker("AAPL", "portfolio", name="Apple Inc.",
                               block="Tecnología", region="USA", horizon="largo")
        meta = database.get_ticker_meta("AAPL")
        assert meta is not None
        assert meta["name"] == "Apple Inc."
        assert meta["horizon"] == "largo"
        assert meta["category"] == "portfolio"

    def test_ticker_exists(self, tmp_db):
        assert database.ticker_exists("AAPL") is False
        database.upsert_ticker("AAPL", "portfolio")
        assert database.ticker_exists("AAPL") is True

    def test_delete_ticker(self, tmp_db):
        database.upsert_ticker("DEL", "watchlist")
        database.delete_ticker_record("DEL")
        assert database.ticker_exists("DEL") is False

    def test_update_ticker_fields(self, tmp_db):
        database.upsert_ticker("NVDA", "watchlist", horizon="corto")
        database.update_ticker_fields("NVDA", horizon="medio", notes="Chips IA")
        meta = database.get_ticker_meta("NVDA")
        assert meta["horizon"] == "medio"
        assert meta["notes"] == "Chips IA"

    def test_get_all_tickers(self, tmp_db):
        database.upsert_ticker("T1", "portfolio")
        database.upsert_ticker("T2", "watchlist")
        rows = database.get_all_tickers()
        assert len(rows) == 2

    def test_upsert_does_not_overwrite_with_none(self, tmp_db):
        database.upsert_ticker("XYZ", "portfolio", name="OriginalName")
        # Segundo upsert sin nombre → no debe borrar el existente
        database.upsert_ticker("XYZ", "portfolio")
        meta = database.get_ticker_meta("XYZ")
        assert meta["name"] == "OriginalName"


# ── price alerts ───────────────────────────────────────────────────────────────

class TestPriceAlerts:
    def test_add_and_get_active(self, tmp_db):
        database.add_price_alert("AAPL", 150.0, "above")
        alerts = database.get_active_alerts()
        assert len(alerts) == 1
        assert alerts[0][1] == "AAPL"  # ticker
        assert alerts[0][2] == 150.0   # target_price

    def test_deactivate_alert(self, tmp_db):
        database.add_price_alert("MSFT", 200.0, "below")
        alerts = database.get_active_alerts()
        alert_id = alerts[0][0]
        database.deactivate_alert(alert_id)
        assert database.get_active_alerts() == []

    def test_expired_alert_not_returned(self, tmp_db):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        database.add_price_alert("GOOG", 100.0, "above", expires_at=yesterday)
        assert database.get_active_alerts() == []

    def test_future_expiry_is_returned(self, tmp_db):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        database.add_price_alert("TSLA", 250.0, "above", expires_at=tomorrow)
        assert len(database.get_active_alerts()) == 1

    def test_condition_type_stored(self, tmp_db):
        database.add_price_alert("AMZN", 0.0, "below",
                                 condition_type="stoploss_pct", condition_value=10.0)
        alert = database.get_active_alerts()[0]
        assert alert[5] == "stoploss_pct"   # condition_type
        assert alert[6] == 10.0             # condition_value


# ── alert history ──────────────────────────────────────────────────────────────

class TestAlertHistory:
    def test_log_and_get_unnotified(self, tmp_db):
        hid = database.log_alert_triggered("AAPL", 150.0, "above", 155.0)
        unnotified = database.get_unnotified_alerts()
        assert len(unnotified) == 1
        assert unnotified[0][0] == hid

    def test_mark_notified_removes_from_unnotified(self, tmp_db):
        hid = database.log_alert_triggered("AAPL", 150.0, "above", 155.0)
        database.mark_alert_notified(hid)
        assert database.get_unnotified_alerts() == []

    def test_get_alert_history(self, tmp_db):
        database.log_alert_triggered("X", 10.0, "above", 11.0)
        database.log_alert_triggered("Y", 20.0, "below", 19.0)
        history = database.get_alert_history()
        assert len(history) == 2


# ── reports ────────────────────────────────────────────────────────────────────

class TestReports:
    def test_save_and_get(self, tmp_db):
        database.save_report("Informe de prueba")
        reports = database.get_recent_reports(n=5)
        assert len(reports) == 1
        assert reports[0][2] == "Informe de prueba"

    def test_count_reports(self, tmp_db):
        assert database.count_reports() == 0
        database.save_report("R1")
        database.save_report("R2")
        assert database.count_reports() == 2

    def test_pagination(self, tmp_db):
        for i in range(5):
            database.save_report(f"Informe {i}")
        page1 = database.get_recent_reports(n=3, offset=0)
        page2 = database.get_recent_reports(n=3, offset=3)
        assert len(page1) == 3
        assert len(page2) == 2


# ── operations ────────────────────────────────────────────────────────────────

class TestOperations:
    def test_add_and_get(self, tmp_db):
        op_id = database.add_operation("AAPL", "2025-01-10", "buy", 10, 150.0)
        ops = database.get_operations()
        assert len(ops) == 1
        assert ops[0][0] == op_id
        assert ops[0][1] == "AAPL"
        assert ops[0][3] == "buy"

    def test_delete_operation(self, tmp_db):
        op_id = database.add_operation("MSFT", "2025-02-01", "sell", 5, 200.0)
        database.delete_operation(op_id)
        assert database.get_operations() == []

    def test_filter_by_ticker(self, tmp_db):
        database.add_operation("AAPL", "2025-01-01", "buy", 10, 100.0)
        database.add_operation("GOOG", "2025-01-02", "buy", 3, 200.0)
        ops = database.get_operations(ticker="AAPL")
        assert all(o[1] == "AAPL" for o in ops)

    def test_count_operations(self, tmp_db):
        assert database.count_operations() == 0
        database.add_operation("X", "2025-01-01", "buy", 1, 10.0)
        assert database.count_operations() == 1

    def test_commission_stored(self, tmp_db):
        database.add_operation("Y", "2025-01-01", "buy", 1, 50.0,
                               commission_eur=2.5)
        ops = database.get_operations()
        assert ops[0][7] == 2.5  # commission_eur (índice 7)


# ── portfolio value ────────────────────────────────────────────────────────────

class TestPortfolioValue:
    def test_save_and_get_history(self, tmp_db):
        database.save_portfolio_value(10000.0, positions_count=5)
        history = database.get_portfolio_value_history(days=7)
        assert len(history) == 1
        assert history[0][1] == 10000.0
        assert history[0][2] == 5

    def test_upsert_same_day(self, tmp_db):
        database.save_portfolio_value(10000.0)
        database.save_portfolio_value(11000.0)
        history = database.get_portfolio_value_history(days=7)
        assert len(history) == 1
        assert history[0][1] == 11000.0


# ── news cache ────────────────────────────────────────────────────────────────

class TestNewsCache:
    def test_cache_and_retrieve(self, tmp_db):
        database.cache_news_translation("Apple reports record earnings", "Apple reporta beneficios récord")
        result = database.get_cached_translation("Apple reports record earnings")
        assert result == "Apple reporta beneficios récord"

    def test_missing_headline_returns_none(self, tmp_db):
        assert database.get_cached_translation("headline no cacheado") is None

    def test_same_headline_updates_translation(self, tmp_db):
        database.cache_news_translation("headline", "v1")
        database.cache_news_translation("headline", "v2")
        assert database.get_cached_translation("headline") == "v2"


# ── push subscriptions ────────────────────────────────────────────────────────

class TestPushSubscriptions:
    def test_upsert_and_get_all(self, tmp_db):
        database.upsert_push_subscription(
            "https://fcm.example.com/sub1", "p256dh_aaa", "auth_bbb"
        )
        subs = database.get_all_push_subscriptions()
        assert len(subs) == 1
        assert subs[0][0] == "https://fcm.example.com/sub1"

    def test_delete_subscription(self, tmp_db):
        ep = "https://fcm.example.com/sub2"
        database.upsert_push_subscription(ep, "p1", "a1")
        database.delete_push_subscription(ep)
        assert database.get_all_push_subscriptions() == []

    def test_upsert_updates_keys(self, tmp_db):
        ep = "https://fcm.example.com/sub3"
        database.upsert_push_subscription(ep, "old_p256dh", "old_auth")
        database.upsert_push_subscription(ep, "new_p256dh", "new_auth")
        subs = database.get_all_push_subscriptions()
        assert subs[0][1] == "new_p256dh"


# ── market discoveries ────────────────────────────────────────────────────────

class TestDiscoveries:
    def _sample_row(self, ticker="AAPL", horizon="largo", rank=1):
        return {
            "ticker": ticker,
            "name": f"{ticker} Inc.",
            "sector": "Tech",
            "region": "USA",
            "horizon": horizon,
            "score": 18.5,
            "opportunity": "ALTA",
            "price_eur": 150.0,
            "drawdown_52w": -15.0,
            "momentum_3m": -5.0,
            "volatility": 20.0,
            "rsi": 28.0,
            "dividend_yield": 0.5,
            "roe": 30.0,
            "pe_ratio": 25.0,
            "analyst_target_eur": 180.0,
            "analyst_rec": 2.0,
            "analyst_n": 40,
            "upside_pct": 20.0,
            "market_cap_b": 2500.0,
            "claude_analysis": "Buena calidad de negocio.",
            "rank_in_horizon": rank,
            "generated_at": datetime.utcnow().isoformat(),
        }

    def test_save_and_get(self, tmp_db):
        rows = [self._sample_row("AAPL", "largo", 1),
                self._sample_row("MSFT", "largo", 2)]
        database.save_discoveries(rows)
        result = database.get_discoveries()
        assert len(result) == 2
        tickers = [r["ticker"] for r in result]
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_save_replaces_previous(self, tmp_db):
        database.save_discoveries([self._sample_row("OLD")])
        database.save_discoveries([self._sample_row("NEW")])
        result = database.get_discoveries()
        assert len(result) == 1
        assert result[0]["ticker"] == "NEW"

    def test_get_discoveries_generated_at(self, tmp_db):
        assert database.get_discoveries_generated_at() is None
        database.save_discoveries([self._sample_row()])
        ts = database.get_discoveries_generated_at()
        assert ts is not None
        assert "T" in ts  # formato ISO

    def test_empty_discoveries(self, tmp_db):
        assert database.get_discoveries() == []


# ── price history / snapshots ─────────────────────────────────────────────────

class TestPriceHistory:
    def _sample_snapshot(self, ticker="AAPL", date_str=None):
        return {
            "ticker": ticker,
            "date": date_str or date.today().isoformat(),
            "price": 150.0,
            "drawdown_52w": -10.0,
            "momentum_3m": 5.0,
            "momentum_6m": 8.0,
            "volatility": 20.0,
            "dividend_yield": 0.5,
            "rsi": 45.0,
            "score": 12.0,
            "opportunity": "MEDIA",
            "category": "portfolio",
            "name": "Apple Inc.",
            "block": "Tecnología",
            "region": "USA",
            "horizon": "largo",
            "target_weight": 10.0,
            "target_price": 170.0,
            "pe_ratio": 28.0,
            "pb_ratio": 5.0,
            "profit_margin": 25.0,
            "roe": 35.0,
            "debt_equity": 1.2,
            "revenue_growth": 8.0,
            "market_cap_b": 2500.0,
            "analyst_rec": 2.0,
            "analyst_target": 180.0,
            "analyst_n": 45,
            "trend": "neutral",
            "pnl": 500.0,
        }

    def test_save_and_get_latest_snapshot(self, tmp_db):
        database.save_snapshot([self._sample_snapshot("AAPL")])
        df = database.get_latest_snapshot_as_df()
        assert df is not None
        assert "AAPL" in df["ticker"].values

    def test_latest_snapshot_returns_most_recent(self, tmp_db):
        database.save_snapshot([self._sample_snapshot("AAPL", "2025-01-01")])
        database.save_snapshot([self._sample_snapshot("AAPL", "2025-01-02")])
        df = database.get_latest_snapshot_as_df()
        assert df[df["ticker"] == "AAPL"]["date"].iloc[0] == "2025-01-02"

    def test_empty_history_returns_none(self, tmp_db):
        assert database.get_latest_snapshot_as_df() is None

    def test_get_ticker_history(self, tmp_db):
        database.save_snapshot([self._sample_snapshot("NVDA", "2025-01-01")])
        database.save_snapshot([self._sample_snapshot("NVDA", "2025-01-02")])
        history = database.get_ticker_history("NVDA", days=10)
        assert len(history) == 2
