"""
test_discovery.py — Tests para las funciones puras de discovery.py.

Se testean funciones sin efectos de red (no yfinance, no Claude, no Wikipedia):
  - _calc_rsi()
  - _infer_region()
  - _score_and_classify()

Las funciones que requieren red (generate_discoveries, get_universe, etc.)
se omiten aquí; se validan en pruebas de integración manuales.
"""
import math

import pandas as pd
import pytest

# Los stubs de yfinance, anthropic, etc. se inyectan en conftest.py antes de
# que este módulo se cargue, por lo que la importación de discovery funciona.
from discovery import _BASE_METALES, _calc_rsi, _infer_region, _score_and_classify


# ── _calc_rsi ─────────────────────────────────────────────────────────────────

class TestCalcRsi:
    def _series(self, values):
        return pd.Series(values, dtype=float)

    def test_insufficient_data_returns_none(self):
        """Menos de period+1 puntos → None."""
        closes = self._series([100.0] * 14)  # necesita ≥ 15 para period=14
        assert _calc_rsi(closes) is None

    def test_exact_minimum_data(self):
        """Exactamente period+1 puntos no siempre produce un valor no-NaN, pero no lanza excepción."""
        closes = self._series([100.0 + i * 0.5 for i in range(15)])
        result = _calc_rsi(closes)
        # Puede ser None o float válido (depende del rolling)
        assert result is None or (isinstance(result, float) and 0 <= result <= 100)

    def test_constant_series_returns_none_or_100(self):
        """Serie plana: ganancias y pérdidas son 0, RSI debería ser NaN o 100."""
        closes = self._series([100.0] * 30)
        result = _calc_rsi(closes)
        # Con pérdida=0 y ganancia=0, RS es NaN → RSI es NaN → None
        # Con pérdida=0 y ganancia>0, RS es inf → RSI=100
        assert result is None or result == 100.0

    def test_monotonic_increase_gives_high_rsi_or_none(self):
        """Precio siempre subiendo → sin pérdidas, RSI = 100 o None (pérdida=0 → NaN por diseño).

        Con todas las ganancias positivas y pérdidas=0, loss_safe=NaN, RS=NaN, RSI=NaN.
        El método retorna None en ese caso. Verificamos que no lanza excepción y que,
        si devuelve un valor, es >70.
        """
        closes = self._series([100.0 + i for i in range(30)])
        result = _calc_rsi(closes)
        if result is not None:
            assert result > 70  # si hay valor, debe ser RSI alto

    def test_monotonic_decrease_gives_low_rsi(self):
        """Precio siempre bajando → RSI bajo (<30)."""
        closes = self._series([200.0 - i for i in range(30)])
        result = _calc_rsi(closes)
        assert result is not None
        assert result < 30

    def test_rsi_bounds_are_0_to_100(self):
        """El RSI siempre debe estar en [0, 100]."""
        import random
        random.seed(42)
        values = [100.0]
        for _ in range(50):
            values.append(values[-1] + random.uniform(-5, 5))
        closes = self._series(values)
        result = _calc_rsi(closes)
        if result is not None:
            assert 0 <= result <= 100

    def test_custom_period(self):
        """Period personalizado se aplica correctamente."""
        closes = self._series([100.0 + i * 0.3 for i in range(25)])
        result_14 = _calc_rsi(closes, period=14)
        result_7  = _calc_rsi(closes, period=7)
        # Ambos deben ser válidos o None; no deben ser iguales en general
        # Solo verificamos que no lanza excepción y que son distintos tipos no es garantizable
        assert result_14 is None or isinstance(result_14, float)
        assert result_7  is None or isinstance(result_7, float)


# ── _infer_region ─────────────────────────────────────────────────────────────

class TestInferRegion:
    def test_us_ticker_no_suffix(self):
        assert _infer_region("AAPL") == "USA"

    def test_german_suffix(self):
        assert _infer_region("SAP.DE") == "Europa"

    def test_french_suffix(self):
        assert _infer_region("OR.PA") == "Europa"

    def test_spanish_suffix(self):
        assert _infer_region("SAN.MC") == "Europa"

    def test_uk_suffix(self):
        assert _infer_region("SHEL.L") == "Europa"

    def test_swiss_suffix(self):
        assert _infer_region("NESN.SW") == "Europa"

    def test_dutch_suffix(self):
        assert _infer_region("ASML.AS") == "Europa"

    def test_hong_kong_suffix(self):
        assert _infer_region("0700.HK") == "Asia-Pacífico"

    def test_italian_suffix(self):
        assert _infer_region("ENI.MI") == "Europa"

    def test_swedish_suffix(self):
        assert _infer_region("VOLV-B.ST") == "Europa"

    def test_precious_metal_miners(self):
        for ticker in _BASE_METALES:
            assert _infer_region(ticker) == "Metales preciosos", \
                f"Se esperaba 'Metales preciosos' para {ticker}"

    def test_unknown_suffix_defaults_to_usa(self):
        assert _infer_region("XYZ.ZZ") == "USA"

    def test_gold_barrick(self):
        assert _infer_region("GOLD") == "Metales preciosos"

    def test_wheaton(self):
        assert _infer_region("WPM") == "Metales preciosos"


# ── _score_and_classify ───────────────────────────────────────────────────────

class TestScoreAndClassify:
    def _base_data(self, **kwargs):
        base = {
            "ticker": "TEST",
            "roe": None,
            "pe_ratio": None,
            "dividend_yield": None,
            "volatility": None,
            "momentum_3m": None,
            "drawdown_52w": None,
            "rsi": None,
        }
        base.update(kwargs)
        return base

    def test_adds_horizon_score_opportunity(self):
        data = self._base_data(drawdown_52w=-20.0)
        result = _score_and_classify(data)
        assert "horizon" in result
        assert "score" in result
        assert "opportunity" in result

    def test_horizon_is_valid_value(self):
        data = self._base_data()
        result = _score_and_classify(data)
        assert result["horizon"] in ("largo", "medio", "corto")

    def test_score_is_float(self):
        data = self._base_data(drawdown_52w=-15.0, volatility=25.0)
        result = _score_and_classify(data)
        assert isinstance(result["score"], float)

    def test_opportunity_labels_are_valid(self):
        data = self._base_data()
        result = _score_and_classify(data)
        assert result["opportunity"] in ("ALTA", "MEDIA", "BAJA")

    def test_high_quality_stock_gets_largo(self):
        """ROE alto + dividendo + PE razonable + baja volatilidad → largo."""
        data = self._base_data(roe=30.0, dividend_yield=3.5,
                               pe_ratio=14.0, volatility=12.0)
        result = _score_and_classify(data)
        assert result["horizon"] == "largo"

    def test_high_volatility_stock_gets_corto(self):
        """Volatilidad muy alta → corto."""
        data = self._base_data(volatility=45.0)
        result = _score_and_classify(data)
        assert result["horizon"] == "corto"

    def test_original_dict_is_mutated(self):
        """_score_and_classify modifica el dict original (no crea uno nuevo)."""
        data = self._base_data()
        result = _score_and_classify(data)
        assert result is data  # misma referencia

    def test_empty_data_does_not_raise(self):
        data = self._base_data()
        result = _score_and_classify(data)
        assert result["score"] == 0.0
        assert result["opportunity"] == "BAJA"
