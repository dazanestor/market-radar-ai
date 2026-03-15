import pandas as pd

def score_watchlist(df):
    def compute_score(row):
        score = 0

        # Drawdown: más caído desde máximo = más oportunidad (peso 30%)
        if pd.notna(row.get("drawdown_52w")):
            score += (-row["drawdown_52w"]) * 0.3

        # Momentum 3m inverso: caída reciente puede ser punto de entrada (peso 15%)
        if pd.notna(row.get("momentum_3m")):
            score += (-row["momentum_3m"]) * 0.15

        # Volatilidad: menor volatilidad = negocio más predecible (peso 15%)
        if pd.notna(row.get("volatility")):
            score += max(0, 30 - row["volatility"]) * 0.15

        # Dividendos: rentabilidad por dividendo suma calidad (peso 15%)
        if pd.notna(row.get("dividend_yield")):
            score += row["dividend_yield"] * 0.15

        # ROE alto: calidad del negocio estilo Buffett (peso 15%)
        # ROE > 15% se considera bueno; normalizamos sobre 30% como techo
        if pd.notna(row.get("roe")) and row["roe"] > 0:
            score += min(row["roe"], 30) * 0.15

        # PER bajo: margen de seguridad en valoración (peso 10%)
        # PER < 30 suma; cuanto más bajo mejor; invertido y normalizado
        if pd.notna(row.get("pe_ratio")) and 0 < row["pe_ratio"] < 60:
            score += max(0, 30 - row["pe_ratio"]) * 0.1

        return round(score, 2)

    _SCORED_COLS = ["drawdown_52w", "momentum_3m", "volatility",
                    "dividend_yield", "roe", "pe_ratio"]

    def _has_data(row) -> bool:
        return any(pd.notna(row.get(c)) for c in _SCORED_COLS)

    df = df.copy()
    df["score"] = df.apply(compute_score, axis=1)
    df["opportunity"] = df.apply(
        lambda row: (
            "ALTA"  if row["score"] > 15 else
            "MEDIA" if row["score"] > 8  else
            "BAJA"
        ) if _has_data(row) else "—",
        axis=1,
    )
    return df
