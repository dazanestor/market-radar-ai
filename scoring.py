import pandas as pd

# ── Descripción de horizontes ─────────────────────────────────────────────────
HORIZON_META = {
    "corto": {
        "label":    "Corto plazo",
        "range":    "días – 3 meses",
        "desc":     "Movimientos técnicos, rebotes y catalizadores inmediatos. "
                    "Señales de sobreventa y momentum.",
        "color":    "badge-red",
        "emoji":    "⚡",
    },
    "medio": {
        "label":    "Medio plazo",
        "range":    "3 meses – 18 meses",
        "desc":     "Combinación de fundamentos y momentum. Ciclos sectoriales "
                    "y recuperación de drawdowns.",
        "color":    "badge-yellow",
        "emoji":    "📈",
    },
    "largo": {
        "label":    "Largo plazo",
        "range":    "18 meses – varios años",
        "desc":     "Calidad del negocio, ventaja competitiva y dividendo creciente. "
                    "Acumulación en caídas.",
        "color":    "badge-blue",
        "emoji":    "🏦",
    },
}

# ── Pesos por horizonte ────────────────────────────────────────────────────────
# Cada horizonte prioriza factores distintos.
# largo: calidad del negocio + valoración + dividendo
# medio: equilibrio fundamentales + momentum
# corto: señales técnicas de sobreventa/rebote (drawdown, momentum, RSI)

_WEIGHTS = {
    "largo": {
        "drawdown_52w":   0.20,
        "momentum_3m":    0.05,
        "volatility":     0.15,   # baja vol = negocio predecible
        "dividend_yield": 0.20,
        "roe":            0.25,
        "pe_ratio":       0.15,
        "rsi":            0.00,
    },
    "medio": {
        "drawdown_52w":   0.25,
        "momentum_3m":    0.15,
        "volatility":     0.10,
        "dividend_yield": 0.10,
        "roe":            0.15,
        "pe_ratio":       0.15,
        "rsi":            0.10,
    },
    "corto": {
        "drawdown_52w":   0.25,
        "momentum_3m":    0.20,
        "volatility":     0.05,
        "dividend_yield": 0.00,
        "roe":            0.00,
        "pe_ratio":       0.00,
        "rsi":            0.50,   # RSI: señal dominante en corto plazo
    },
}

_SCORED_COLS = ["drawdown_52w", "momentum_3m", "volatility",
                "dividend_yield", "roe", "pe_ratio", "rsi"]


def _has_data(row) -> bool:
    return any(pd.notna(row.get(c)) for c in _SCORED_COLS)


def _compute_score(row, weights: dict) -> float:
    score = 0.0

    # Drawdown: mayor caída desde máximo = más oportunidad (señal contraria)
    if pd.notna(row.get("drawdown_52w")):
        score += (-row["drawdown_52w"]) * weights["drawdown_52w"]

    # Momentum 3m inverso: caída reciente puede ser punto de entrada
    if pd.notna(row.get("momentum_3m")):
        score += (-row["momentum_3m"]) * weights["momentum_3m"]

    # Volatilidad: menor = negocio más predecible (relevante en largo plazo)
    if pd.notna(row.get("volatility")) and weights["volatility"] > 0:
        score += max(0, 30 - row["volatility"]) * weights["volatility"]

    # Dividendo: retribución al accionista
    if pd.notna(row.get("dividend_yield")) and weights["dividend_yield"] > 0:
        score += row["dividend_yield"] * weights["dividend_yield"]

    # ROE: calidad del negocio (cap en 30%)
    if pd.notna(row.get("roe")) and row["roe"] > 0 and weights["roe"] > 0:
        score += min(row["roe"], 30) * weights["roe"]

    # PER: valoración (cuanto más bajo, mejor; rango 0-60)
    if pd.notna(row.get("pe_ratio")) and 0 < row["pe_ratio"] < 60 and weights["pe_ratio"] > 0:
        score += max(0, 30 - row["pe_ratio"]) * weights["pe_ratio"]

    # RSI: sobreventa extrema = señal de rebote (RSI < 30 aporta máximo)
    if pd.notna(row.get("rsi")) and weights["rsi"] > 0:
        # RSI 0 → +15 pts; RSI 30 → 0 pts; RSI > 30 → 0 pts
        score += max(0, 30 - row["rsi"]) * weights["rsi"]

    return round(score, 2)


def _opportunity_label(score: float) -> str:
    if score > 15:
        return "ALTA"
    if score > 8:
        return "MEDIA"
    return "BAJA"


def score_watchlist(df):
    """Scoring genérico (horizonte neutro) para compatibilidad con código existente."""
    df = df.copy()
    weights = _WEIGHTS["medio"]
    df["score"] = df.apply(lambda row: _compute_score(row, weights), axis=1)
    df["opportunity"] = df.apply(
        lambda row: _opportunity_label(row["score"]) if _has_data(row) else "—",
        axis=1,
    )
    return df


def score_by_horizon(df):
    """Calcula score y oportunidad usando los pesos específicos del horizonte de cada fila.

    Si una fila no tiene horizonte definido, usa los pesos 'medio'.
    Añade columnas: score_h, opportunity_h (h = horizonte).
    """
    df = df.copy()

    def _row_score(row):
        h = row.get("horizon") or "medio"
        if h not in _WEIGHTS:
            h = "medio"
        return _compute_score(row, _WEIGHTS[h])

    df["score"] = df.apply(_row_score, axis=1)
    df["opportunity"] = df.apply(
        lambda row: _opportunity_label(row["score"]) if _has_data(row) else "—",
        axis=1,
    )
    return df


def suggest_horizon(roe, pe_ratio, dividend_yield, volatility, momentum_3m) -> str:
    """Sugiere horizonte de inversión basado en características del activo.

    Retorna: 'largo', 'medio' o 'corto'.
    """
    largo = 0
    medio = 0
    corto = 0

    # Calidad del negocio → largo plazo
    if roe and roe > 15:
        largo += 3
    if roe and roe > 25:
        largo += 2
    if dividend_yield and dividend_yield > 2:
        largo += 2
    if pe_ratio and 8 < pe_ratio < 22:
        largo += 2
    if volatility and volatility < 18:
        largo += 2

    # Volatilidad moderada + fundamentales medios → medio plazo
    if volatility and 18 <= volatility <= 35:
        medio += 3
    if pe_ratio and 22 <= pe_ratio < 40:
        medio += 2
    if roe and 8 <= roe <= 15:
        medio += 2

    # Alta volatilidad + momentum extremo → corto plazo
    if volatility and volatility > 35:
        corto += 3
    if momentum_3m and momentum_3m < -20:
        corto += 2  # caída fuerte → rebote técnico potencial
    if momentum_3m and momentum_3m > 25:
        corto += 2  # tendencia fuerte al alza

    scores = {"largo": largo, "medio": medio, "corto": corto}
    return max(scores, key=scores.get)
