"""
test_scoring.py — Tests unitarios para scoring.py.

Todos los tests son sobre funciones puras: no requieren BD ni red.
"""
import math

import pandas as pd
import pytest

from scoring import (
    _WEIGHTS,
    _compute_score,
    _has_data,
    _opportunity_label,
    get_weights,
    score_by_horizon,
    score_watchlist,
    suggest_horizon,
)

# ── _has_data ─────────────────────────────────────────────────────────────────

class TestHasData:
    def test_row_with_drawdown_is_true(self):
        assert _has_data({"drawdown_52w": -15.0}) is True

    def test_row_with_rsi_is_true(self):
        assert _has_data({"rsi": 45.0}) is True

    def test_empty_row_is_false(self):
        assert _has_data({}) is False

    def test_none_values_is_false(self):
        row = {c: None for c in ["drawdown_52w", "momentum_3m", "volatility",
                                  "dividend_yield", "roe", "pe_ratio", "rsi"]}
        assert _has_data(row) is False

    def test_nan_values_is_false(self):
        row = {c: float("nan") for c in ["drawdown_52w", "momentum_3m"]}
        assert _has_data(row) is False

    def test_mix_none_and_value(self):
        assert _has_data({"drawdown_52w": None, "roe": 20.0}) is True


# ── _compute_score ────────────────────────────────────────────────────────────

class TestComputeScore:
    def test_empty_row_returns_zero(self):
        assert _compute_score({}, _WEIGHTS["medio"]) == 0.0

    def test_deep_drawdown_adds_positive_score(self):
        # drawdown -30% → contribución positiva (señal de oportunidad)
        row = {"drawdown_52w": -30.0}
        score = _compute_score(row, _WEIGHTS["medio"])
        expected = 30.0 * 0.25
        assert abs(score - expected) < 0.01

    def test_low_volatility_adds_score_for_largo(self):
        # vol 10% → (30-10) * 0.15 = 3.0
        row = {"volatility": 10.0}
        score = _compute_score(row, _WEIGHTS["largo"])
        assert abs(score - 3.0) < 0.01

    def test_high_volatility_above_30_still_contributes_zero(self):
        row = {"volatility": 50.0}
        score = _compute_score(row, _WEIGHTS["largo"])
        assert score == 0.0  # max(0, 30-50) = 0

    def test_good_roe_capped_at_30(self):
        # ROE 100% → capped at 30 → 30 * 0.25 = 7.5
        row = {"roe": 100.0}
        score = _compute_score(row, _WEIGHTS["largo"])
        assert abs(score - 7.5) < 0.01

    def test_negative_roe_not_scored(self):
        row = {"roe": -5.0}
        score = _compute_score(row, _WEIGHTS["largo"])
        assert score == 0.0

    def test_pe_above_60_not_scored(self):
        row = {"pe_ratio": 80.0}
        score = _compute_score(row, _WEIGHTS["largo"])
        assert score == 0.0

    def test_pe_zero_not_scored(self):
        row = {"pe_ratio": 0.0}
        score = _compute_score(row, _WEIGHTS["largo"])
        assert score == 0.0

    def test_low_pe_adds_score(self):
        # PE 10 → (30-10) * 0.15 = 3.0
        row = {"pe_ratio": 10.0}
        score = _compute_score(row, _WEIGHTS["largo"])
        assert abs(score - 3.0) < 0.01

    def test_rsi_below_30_adds_max_score_corto(self):
        # RSI 20 → (30-20) * 0.50 = 5.0
        row = {"rsi": 20.0}
        score = _compute_score(row, _WEIGHTS["corto"])
        assert abs(score - 5.0) < 0.01

    def test_rsi_above_30_not_scored(self):
        row = {"rsi": 50.0}
        score = _compute_score(row, _WEIGHTS["corto"])
        assert score == 0.0

    def test_rsi_ignored_in_largo(self):
        row = {"rsi": 10.0}
        score = _compute_score(row, _WEIGHTS["largo"])
        # peso RSI en largo = 0
        assert score == 0.0

    def test_dividend_yield_ignored_in_corto(self):
        row = {"dividend_yield": 5.0}
        score = _compute_score(row, _WEIGHTS["corto"])
        assert score == 0.0

    def test_result_is_rounded_to_2_decimals(self):
        row = {"drawdown_52w": -10.123456}
        score = _compute_score(row, _WEIGHTS["medio"])
        assert score == round(score, 2)

    def test_full_row_largo(self):
        """Comprobación de suma completa para horizonte largo."""
        row = {
            "drawdown_52w":   -20.0,   # 20 * 0.20 = 4.0
            "momentum_3m":    -5.0,    # 5 * 0.05 = 0.25
            "volatility":      15.0,   # (30-15) * 0.15 = 2.25
            "dividend_yield":   3.0,   # 3 * 0.20 = 0.60
            "roe":             20.0,   # 20 * 0.25 = 5.0
            "pe_ratio":        12.0,   # (30-12) * 0.15 = 2.70
            "rsi":             45.0,   # peso = 0
        }
        expected = 4.0 + 0.25 + 2.25 + 0.60 + 5.0 + 2.70
        score = _compute_score(row, _WEIGHTS["largo"])
        assert abs(score - expected) < 0.01


# ── _opportunity_label ────────────────────────────────────────────────────────

class TestOpportunityLabel:
    @pytest.mark.parametrize("score,expected", [
        (20.0, "ALTA"),
        (15.1, "ALTA"),
        (15.0, "MEDIA"),   # not strictly > 15
        (10.0, "MEDIA"),
        (8.1,  "MEDIA"),
        (8.0,  "BAJA"),
        (0.0,  "BAJA"),
    ])
    def test_boundaries(self, score, expected):
        assert _opportunity_label(score) == expected


# ── suggest_horizon ────────────────────────────────────────────────────────────

class TestSuggestHorizon:
    def test_high_quality_business_is_largo(self):
        # ROE 25, PE 15, div 3%, vol 12%
        h = suggest_horizon(roe=25, pe_ratio=15, dividend_yield=3.0,
                            volatility=12, momentum_3m=0)
        assert h == "largo"

    def test_high_volatility_is_corto(self):
        h = suggest_horizon(roe=None, pe_ratio=None, dividend_yield=None,
                            volatility=40, momentum_3m=0)
        assert h == "corto"

    def test_strong_downtrend_with_high_vol_is_corto(self):
        # volatility>35 (+3 corto) + momentum<-20 (+2 corto) → corto domina
        h = suggest_horizon(roe=None, pe_ratio=None, dividend_yield=None,
                            volatility=40, momentum_3m=-25)
        assert h == "corto"

    def test_strong_uptrend_with_high_vol_is_corto(self):
        # volatility>35 (+3 corto) + momentum>25 (+2 corto) → corto domina
        h = suggest_horizon(roe=None, pe_ratio=None, dividend_yield=None,
                            volatility=40, momentum_3m=30)
        assert h == "corto"

    def test_moderate_vol_with_extreme_momentum_prefers_medio(self):
        # volatility 18-35 da +3 a medio; momentum<-20 da +2 a corto → medio gana
        h = suggest_horizon(roe=None, pe_ratio=None, dividend_yield=None,
                            volatility=20, momentum_3m=-25)
        assert h == "medio"

    def test_moderate_fundamentals_is_medio(self):
        h = suggest_horizon(roe=10, pe_ratio=25, dividend_yield=1.0,
                            volatility=22, momentum_3m=5)
        assert h == "medio"

    def test_all_none_returns_string(self):
        h = suggest_horizon(None, None, None, None, None)
        assert h in ("largo", "medio", "corto")


# ── get_weights ───────────────────────────────────────────────────────────────

class TestGetWeights:
    def test_returns_copy_not_original(self):
        w = get_weights("largo")
        w["roe"] = 999.0
        assert _WEIGHTS["largo"]["roe"] != 999.0

    def test_unknown_horizon_falls_back_to_medio(self):
        w = get_weights("desconocido")
        assert w == _WEIGHTS["medio"]

    def test_db_override_applies(self):
        w = get_weights("largo", db_override={"roe": 0.5})
        assert w["roe"] == 0.5
        # El resto de campos no cambia
        assert w["drawdown_52w"] == _WEIGHTS["largo"]["drawdown_52w"]

    def test_db_override_ignores_unknown_factor(self):
        w = get_weights("largo", db_override={"factor_inventado": 0.9})
        assert "factor_inventado" not in w


# ── score_watchlist / score_by_horizon ────────────────────────────────────────

class TestScoreFunctions:
    def _make_df(self, rows):
        return pd.DataFrame(rows)

    def test_score_watchlist_adds_score_and_opportunity_columns(self):
        df = self._make_df([
            {"drawdown_52w": -20.0, "momentum_3m": -5.0, "volatility": 20.0,
             "dividend_yield": 2.0, "roe": 18.0, "pe_ratio": 12.0, "rsi": 40.0},
        ])
        result = score_watchlist(df)
        assert "score" in result.columns
        assert "opportunity" in result.columns
        assert result["score"].iloc[0] > 0

    def test_score_watchlist_row_without_data_gets_dash(self):
        df = self._make_df([{"drawdown_52w": None}])
        result = score_watchlist(df)
        assert result["opportunity"].iloc[0] == "—"

    def test_score_by_horizon_uses_per_row_weights(self):
        df = self._make_df([
            {"rsi": 15.0, "horizon": "corto",
             "drawdown_52w": None, "momentum_3m": None, "volatility": None,
             "dividend_yield": None, "roe": None, "pe_ratio": None},
            {"rsi": 15.0, "horizon": "largo",
             "drawdown_52w": None, "momentum_3m": None, "volatility": None,
             "dividend_yield": None, "roe": None, "pe_ratio": None},
        ])
        result = score_by_horizon(df)
        # RSI tiene peso 50% en corto y 0% en largo → score corto >> largo
        score_corto = result[result["horizon"] == "corto"]["score"].iloc[0]
        score_largo = result[result["horizon"] == "largo"]["score"].iloc[0]
        assert score_corto > score_largo

    def test_score_by_horizon_unknown_horizon_uses_medio(self):
        df = self._make_df([
            {"drawdown_52w": -10.0, "horizon": "inexistente",
             "momentum_3m": None, "volatility": None,
             "dividend_yield": None, "roe": None, "pe_ratio": None, "rsi": None},
        ])
        result = score_by_horizon(df)
        expected = _compute_score({"drawdown_52w": -10.0}, _WEIGHTS["medio"])
        assert abs(result["score"].iloc[0] - expected) < 0.01
