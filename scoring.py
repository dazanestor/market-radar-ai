import pandas as pd

def score_watchlist(df):
    df["score"] = (
        (-df["drawdown_52w"]) * 0.6
    )
    df["opportunity"] = df["score"].apply(
        lambda x: "ALTA" if x > 20 else "MEDIA" if x > 10 else "BAJA"
    )
    return df
