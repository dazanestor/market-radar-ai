"""
discovery.py — Descubrimiento de oportunidades de mercado globales.

Pipeline:
  1. Universo: índices bursátiles vía Wikipedia + mineras de metales preciosos.
  2. Fetch paralelo de fundamentales y datos técnicos vía yfinance (período 1y).
  3. Scoring con los mismos pesos que scoring.py (horizon-aware).
  4. Top N por horizonte → análisis cualitativo con Claude.
  5. Resultados guardados en market_discoveries (TTL 24h).

Función principal: generate_discoveries()
"""

import json
import logging
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from database import (
    get_discoveries_generated_at,
    get_setting,
    save_discoveries,
    get_all_tickers,
)
from fetch_data import to_eur
from scoring import _compute_score, _WEIGHTS, _opportunity_label, suggest_horizon

logger = logging.getLogger("discovery")

# ── Constantes ────────────────────────────────────────────────────────────────

_TOP_PER_HORIZON  = 6      # resultados por horizonte (largo/medio/corto)
_UNIVERSE_TTL     = 86400 * 30   # 30 días para refrescar el universo desde Wikipedia
_DISCOVERY_TTL    = 86400        # 24h para refrescar los descubrimientos
_FETCH_WORKERS    = 10

# ── Universo base (hardcoded, siempre disponible como fallback) ───────────────

_BASE_USA = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "BRK-B", "JPM", "JNJ",
    "PG", "KO", "WMT", "V", "MA", "UNH", "HD", "LLY", "ABBV", "MCD", "COST",
    "AVGO", "CVX", "XOM", "PEP", "TMO", "ABT", "NEE", "PM", "RTX", "CSCO",
    "ACN", "ADBE", "TXN", "HON", "INTC", "IBM", "GE", "CAT", "BA", "MMM",
    "GS", "MS", "BAC", "WFC", "BLK", "SPGI", "MCO", "LOW", "SBUX", "AMGN",
    "GILD", "BMY", "REGN", "ISRG", "MDLZ", "CL", "MO", "T", "VZ", "CMCSA",
]

_BASE_EUROPA = [
    # Alemania
    "SAP.DE", "SIE.DE", "ALV.DE", "BMW.DE", "MBG.DE", "BAYN.DE",
    "DTE.DE", "VOW3.DE", "ADS.DE", "BAS.DE", "MRK.DE", "DHL.DE",
    # Francia
    "OR.PA", "MC.PA", "BNP.PA", "SAN.PA", "TTE.PA", "AI.PA",
    "AIR.PA", "CAP.PA", "DSY.PA", "SU.PA", "KER.PA", "HO.PA",
    # España
    "SAN.MC", "IBE.MC", "REP.MC", "ITX.MC", "BBVA.MC", "AMS.MC",
    "TEF.MC", "ELE.MC",
    # Reino Unido
    "SHEL.L", "AZN.L", "ULVR.L", "HSBC.L", "BP.L", "RIO.L",
    "GSK.L", "BATS.L", "DGE.L", "REL.L", "LSEG.L",
    # Países Bajos
    "ASML.AS", "PHIA.AS", "UNA.AS",
    # Suiza
    "NESN.SW", "ROG.SW", "NOVN.SW", "ABBN.SW", "CFR.SW", "ZURN.SW",
    # Dinamarca
    "NOVO-B.CO",
    # Italia
    "ENI.MI", "ISP.MI", "UCG.MI",
    # Suecia
    "VOLV-B.ST", "ERIC-B.ST",
]

_BASE_ASIA = [
    # ADRs USA (sin sufijo, más fiables en yfinance)
    "TSM", "SONY", "TM", "NVO", "MUFG", "SMFG", "SAP",
    # Hong Kong directo
    "0700.HK", "9988.HK", "1299.HK",
]

_BASE_METALES = [
    # Mineras de oro
    "GOLD",   # Barrick Gold
    "NEM",    # Newmont
    "AEM",    # Agnico Eagle
    "KGC",    # Kinross Gold
    "AGI",    # Alamos Gold
    # Royalties (streaming companies)
    "WPM",    # Wheaton Precious Metals
    "FNV",    # Franco-Nevada
    "RGLD",   # Royal Gold
    # Mineras de plata
    "PAAS",   # Pan American Silver
    "AG",     # First Majestic Silver
    "HL",     # Hecla Mining
    # Mineras de platino/paladio
    "SBSW",   # Sibanye Stillwater
    "IMPUY",  # Impala Platinum (ADR)
]

_BASE_UNIVERSE = _BASE_USA + _BASE_EUROPA + _BASE_ASIA + _BASE_METALES


# ── Fetch del universo (Wikipedia + base hardcoded) ───────────────────────────

_WIKI_INDICES = [
    {
        "url":    "https://en.wikipedia.org/wiki/S%26P_100",
        "cols":   ["Ticker", "Symbol"],
        "suffix": "",
        "region": "USA",
    },
    {
        "url":    "https://en.wikipedia.org/wiki/DAX",
        "cols":   ["Ticker", "Ticker symbol"],
        "suffix": ".DE",
        "region": "Europa",
    },
    {
        "url":    "https://en.wikipedia.org/wiki/CAC_40",
        "cols":   ["Ticker", "Symbol"],
        "suffix": ".PA",
        "region": "Europa",
    },
    {
        "url":    "https://en.wikipedia.org/wiki/FTSE_100_Index",
        "cols":   ["Ticker", "EPIC", "Symbol"],
        "suffix": ".L",
        "region": "Europa",
    },
    {
        "url":    "https://en.wikipedia.org/wiki/IBEX_35",
        "cols":   ["Ticker", "Symbol"],
        "suffix": ".MC",
        "region": "Europa",
    },
    {
        "url":    "https://en.wikipedia.org/wiki/Euro_Stoxx_50",
        "cols":   ["Ticker", "Symbol"],
        "suffix": "",   # ya incluyen sufijo en Wikipedia
        "region": "Europa",
    },
]


def _fetch_wikipedia_tickers() -> list:
    """
    Descarga componentes de índices desde Wikipedia.
    Devuelve lista de tickers yfinance válidos.
    Silencia errores individuales y continúa.
    """
    tickers = set()
    for source in _WIKI_INDICES:
        try:
            tables = pd.read_html(source["url"], flavor="lxml")
            for table in tables:
                for col in source["cols"]:
                    if col in table.columns:
                        raw = table[col].dropna().astype(str).tolist()
                        for t in raw:
                            t = t.strip().upper()
                            # Descartar entradas que no parecen tickers
                            if not t or len(t) > 12 or " " in t or t.startswith("0"):
                                continue
                            # Añadir sufijo si el ticker no lo tiene ya
                            suffix = source["suffix"]
                            if suffix and not t.endswith(suffix):
                                t = t + suffix
                            tickers.add(t)
                        break  # columna encontrada, no seguir buscando
        except Exception as e:
            logger.debug("Wikipedia fetch error (%s): %s", source["url"], e)

    return list(tickers)


def get_universe() -> list:
    """
    Devuelve el universo completo de tickers a analizar.
    Orden de prioridad:
      1. settings("discovery_universe") en BD (caché mensual).
      2. Wikipedia + base hardcoded (genera y guarda en BD).
      3. Base hardcoded puro (fallback si Wikipedia falla).
    """
    from database import get_setting, set_setting

    cached_json = get_setting("discovery_universe")
    cached_ts   = get_setting("discovery_universe_ts")
    now_ts      = time.monotonic()

    if cached_json and cached_ts:
        try:
            age = now_ts - float(cached_ts)
            if age < _UNIVERSE_TTL:
                universe = json.loads(cached_json)
                if universe:
                    return universe
        except Exception:
            pass

    # Refrescar desde Wikipedia
    logger.info("Refrescando universo de descubrimiento desde Wikipedia...")
    wiki_tickers = []
    try:
        wiki_tickers = _fetch_wikipedia_tickers()
        logger.info("Wikipedia: %d tickers obtenidos", len(wiki_tickers))
    except Exception as e:
        logger.warning("Error global Wikipedia: %s", e)

    # Combinar con base hardcoded, deduplicar
    combined = list(dict.fromkeys(_BASE_UNIVERSE + wiki_tickers))
    # Guardar en BD
    try:
        set_setting("discovery_universe", json.dumps(combined))
        set_setting("discovery_universe_ts", str(time.monotonic()))
    except Exception:
        pass

    return combined if combined else _BASE_UNIVERSE


def refresh_universe() -> list:
    """Fuerza la regeneración del universo borrando la caché."""
    from database import set_setting
    set_setting("discovery_universe", "")
    set_setting("discovery_universe_ts", "0")
    return get_universe()


# ── Fetch de datos por ticker ─────────────────────────────────────────────────

def _calc_rsi(closes: pd.Series, period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    loss_safe = loss.replace(0, float("nan"))
    rs  = gain / loss_safe
    rsi = 100 - 100 / (1 + rs)
    val = rsi.iloc[-1]
    return float(val) if pd.notna(val) else None


def _fetch_ticker(ticker: str) -> dict | None:
    """
    Descarga datos de un ticker via yfinance y retorna un dict con métricas.
    Retorna None si no hay datos suficientes.
    """
    try:
        stock = yf.Ticker(ticker)
        hist  = stock.history(period="1y")
        if hist.empty or len(hist) < 30:
            return None

        closes = hist["Close"].dropna()
        if len(closes) < 20:
            return None

        price_raw = float(closes.iloc[-1])

        info = {}
        try:
            info = stock.info or {}
        except Exception:
            pass

        currency = info.get("currency") or "USD"

        # Convertir precio a EUR
        price_eur = to_eur(price_raw, currency)
        if not price_eur or math.isnan(price_eur):
            return None

        # Drawdown 52 semanas
        high_52w = float(closes.tail(252).max())
        drawdown_52w = (price_raw / high_52w - 1) * 100 if high_52w > 0 else None

        # Momentum 3m (~63 sesiones)
        idx_3m   = max(0, len(closes) - 63)
        base_3m  = float(closes.iloc[idx_3m])
        momentum_3m = (price_raw / base_3m - 1) * 100 if base_3m > 0 else None

        # Volatilidad anualizada
        rets = closes.pct_change().dropna()
        volatility = float(rets.std() * (252 ** 0.5) * 100) if len(rets) >= 20 else None

        # RSI (14)
        rsi = _calc_rsi(closes)

        # Fundamentales
        raw_div = info.get("dividendYield")
        div_yield = None
        if raw_div is not None:
            try:
                v = float(raw_div)
                # yfinance SIEMPRE devuelve fracción decimal (0.034 = 3.4%).
                # Ningún yield legítimo supera 1.0 en formato fracción (= 100%).
                # Si v >= 1.0 es dato corrupto. Cap conservador en 0.15 (15%).
                if not math.isnan(v) and 0.0 <= v <= 0.15:
                    div_yield = round(v * 100, 2)
            except (ValueError, TypeError):
                pass

        raw_roe = info.get("returnOnEquity")
        roe = None
        if raw_roe is not None:
            try:
                v = float(raw_roe)
                # yfinance SIEMPRE devuelve fracción decimal (1.5 = 150%).
                # Cap en 5.0 fracción (= 500% ROE); más allá es dato corrupto.
                if not math.isnan(v) and abs(v) <= 5.0:
                    roe = round(v * 100, 2)
            except (ValueError, TypeError):
                pass

        raw_pe  = info.get("trailingPE") or info.get("forwardPE")
        pe_ratio = None
        if raw_pe is not None:
            try:
                v = float(raw_pe)
                if 0 < v < 200 and not math.isnan(v):
                    pe_ratio = v
            except Exception:
                pass

        # Analistas
        raw_target  = info.get("targetMeanPrice")
        analyst_rec = info.get("recommendationMean")
        analyst_n   = info.get("numberOfAnalystOpinions")

        analyst_target_eur = None
        upside_pct         = None
        if raw_target:
            try:
                t_eur = to_eur(float(raw_target), currency)
                if t_eur and not math.isnan(t_eur) and price_eur > 0:
                    analyst_target_eur = t_eur
                    upside_pct = (t_eur / price_eur - 1) * 100
            except Exception:
                pass

        # Capitalización (convertida a EUR para comparabilidad entre regiones)
        market_cap = info.get("marketCap")
        market_cap_b = None
        if market_cap:
            try:
                mc_eur = to_eur(float(market_cap), currency)
                if mc_eur and not math.isnan(mc_eur):
                    market_cap_b = mc_eur / 1e9
            except Exception:
                pass

        name   = info.get("shortName") or info.get("longName") or ticker
        sector = info.get("sector") or ""
        # Region por sufijo del ticker
        region = _infer_region(ticker)

        return {
            "ticker":            ticker,
            "name":              name[:80],
            "sector":            sector,
            "region":            region,
            "price_eur":         round(price_eur, 4),
            "drawdown_52w":      round(drawdown_52w, 2) if drawdown_52w is not None else None,
            "momentum_3m":       round(momentum_3m, 2) if momentum_3m is not None else None,
            "volatility":        round(volatility, 2) if volatility is not None else None,
            "rsi":               round(rsi, 2) if rsi is not None else None,
            "dividend_yield":    round(div_yield, 2) if div_yield is not None else None,
            "roe":               round(roe, 2) if roe is not None else None,
            "pe_ratio":          round(pe_ratio, 2) if pe_ratio is not None else None,
            "analyst_target_eur": round(analyst_target_eur, 4) if analyst_target_eur else None,
            "analyst_rec":       round(float(analyst_rec), 2) if analyst_rec else None,
            "analyst_n":         int(analyst_n) if analyst_n else None,
            "upside_pct":        round(upside_pct, 1) if upside_pct is not None else None,
            "market_cap_b":      round(market_cap_b, 1) if market_cap_b else None,
        }
    except Exception as e:
        logger.debug("Error fetching %s: %s", ticker, e)
        return None


_SUFFIX_REGION = {
    ".DE": "Europa", ".PA": "Europa", ".MC": "Europa", ".L": "Europa",
    ".AS": "Europa", ".SW": "Europa", ".CO": "Europa", ".ST": "Europa",
    ".MI": "Europa", ".LS": "Europa", ".BR": "Europa", ".OL": "Europa",
    ".HE": "Europa", ".HK": "Asia-Pacífico", ".T":  "Asia-Pacífico",
    ".AX": "Asia-Pacífico", ".KS": "Asia-Pacífico", ".SS": "Asia-Pacífico",
}

_METAL_TICKERS = set(_BASE_METALES)


def _infer_region(ticker: str) -> str:
    if ticker in _METAL_TICKERS:
        return "Metales preciosos"
    for suffix, region in _SUFFIX_REGION.items():
        if ticker.endswith(suffix):
            return region
    return "USA"


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_and_classify(data: dict) -> dict:
    """Aplica scoring horizon-aware al dict de métricas del ticker."""
    horizon = suggest_horizon(
        data.get("roe"),
        data.get("pe_ratio"),
        data.get("dividend_yield"),
        data.get("volatility"),
        data.get("momentum_3m"),
    )
    weights = _WEIGHTS[horizon]
    score   = _compute_score(data, weights)
    data["horizon"]     = horizon
    data["score"]       = score
    data["opportunity"] = _opportunity_label(score)
    return data


# ── Análisis Claude ───────────────────────────────────────────────────────────

def _claude_analysis(candidates: list) -> dict:
    """
    Envía los candidatos a Claude y devuelve {ticker: análisis}.
    Si Claude falla, retorna dict vacío.
    """
    if not candidates:
        return {}
    try:
        from database import get_setting as _gs
        from config import ANTHROPIC_API_KEY, MODEL
        import anthropic

        api_key = _gs("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY
        model   = _gs("MODEL") or MODEL
        if not api_key:
            return {}

        def _safe(v: str, n: int = 200) -> str:
            """Elimina caracteres de control para prevenir prompt injection (ISO 27001 A.14.2)."""
            return re.sub(r"[\x00-\x1f\x7f]", " ", str(v).strip())[:n]

        lines = []
        for c in candidates:
            h_label = {"largo": "largo plazo", "medio": "medio plazo", "corto": "corto plazo"}.get(c["horizon"], c["horizon"])
            parts = [
                f"- {c['ticker']} ({_safe(c.get('name',''))}), horizonte {h_label}",
                f"  Score: {c['score']:.1f}, Oportunidad: {c['opportunity']}",
            ]
            if c.get("drawdown_52w") is not None:
                parts.append(f"  Drawdown 52s: {c['drawdown_52w']:.1f}%")
            if c.get("upside_pct") is not None:
                parts.append(f"  Upside analistas: {c['upside_pct']:+.1f}%")
            if c.get("analyst_rec") is not None:
                rec_map = {1: "Compra fuerte", 2: "Compra", 3: "Mantener", 4: "Vender", 5: "Vender fuerte"}
                rec_label = rec_map.get(round(c["analyst_rec"]), f"{c['analyst_rec']:.1f}")
                parts.append(f"  Consenso analistas: {rec_label} ({c.get('analyst_n', '?')} analistas)")
            if c.get("roe") is not None:
                parts.append(f"  ROE: {c['roe']:.1f}%")
            if c.get("pe_ratio") is not None:
                parts.append(f"  PER: {c['pe_ratio']:.1f}x")
            if c.get("volatility") is not None:
                parts.append(f"  Volatilidad: {c['volatility']:.1f}%")
            if c.get("rsi") is not None:
                parts.append(f"  RSI(14): {c['rsi']:.1f}")
            if c.get("sector"):
                parts.append(f"  Sector: {_safe(c['sector'])}")
            lines.append("\n".join(parts))

        prompt = (
            "Eres un analista financiero experto. Se te presenta una lista de acciones "
            "seleccionadas cuantitativamente como oportunidades de inversión. Para cada acción "
            "escribe exactamente 2 frases en español:\n"
            "1. Por qué encaja en su horizonte temporal según sus métricas.\n"
            "2. El principal riesgo a vigilar.\n\n"
            "Formato de respuesta estricto (una línea por acción):\n"
            "TICKER: [frase 1] [frase 2]\n\n"
            "Acciones a analizar:\n\n"
            + "\n\n".join(lines)
        )

        client = anthropic.Anthropic(api_key=api_key, timeout=30)  # ISO 27001 A.12 — disponibilidad
        resp   = client.messages.create(
            model=model, max_tokens=1500, temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        result = {}
        for line in resp.content[0].text.strip().split("\n"):
            line = line.strip()
            if ":" in line:
                parts = line.split(":", 1)
                ticker_part = parts[0].strip().upper()
                analysis    = parts[1].strip()
                # Buscar ticker en candidatos (puede tener sufijo)
                for c in candidates:
                    if c["ticker"].upper() == ticker_part or c["ticker"].upper().split(".")[0] == ticker_part:
                        result[c["ticker"]] = analysis
                        break
        return result
    except Exception as e:
        logger.warning("Claude analysis error: %s", e)
        return {}


# ── Pipeline principal ────────────────────────────────────────────────────────

def is_stale() -> bool:
    """True si no hay descubrimientos o tienen más de 24h."""
    ts = get_discoveries_generated_at()
    if not ts:
        return True
    try:
        generated = datetime.fromisoformat(ts)
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - generated).total_seconds()
        return age > _DISCOVERY_TTL
    except Exception:
        return True


def generate_discoveries() -> list:
    """
    Ejecuta el pipeline completo: fetch → score → top N → Claude → guarda en BD.
    Devuelve la lista de dicts guardados.
    """
    logger.info("Iniciando generación de descubrimientos de mercado...")

    # Tickers ya monitorizados (excluir)
    monitored = {r["ticker"].upper() for r in get_all_tickers()}

    # Universo
    universe = get_universe()
    to_fetch = [t for t in universe if t.upper() not in monitored]
    logger.info("Universo: %d tickers (%d ya monitorizados, %d a analizar)",
                len(universe), len(monitored), len(to_fetch))

    # Fetch paralelo
    raw_data = []
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_ticker, t): t for t in to_fetch}
        for fut in as_completed(futures):
            try:
                result = fut.result()
                if result:
                    raw_data.append(result)
            except Exception:
                pass
    logger.info("Fetch completado: %d/%d tickers con datos", len(raw_data), len(to_fetch))

    if not raw_data:
        logger.warning("No se obtuvieron datos; abortando generación.")
        return []

    # Score + clasificación por horizonte
    scored = [_score_and_classify(d) for d in raw_data]

    # Top N por horizonte (solo ALTA o MEDIA)
    by_horizon: dict = {"largo": [], "medio": [], "corto": []}
    for item in sorted(scored, key=lambda x: x["score"], reverse=True):
        h = item["horizon"]
        if h in by_horizon and len(by_horizon[h]) < _TOP_PER_HORIZON:
            if item.get("opportunity") in ("ALTA", "MEDIA"):
                by_horizon[h].append(item)

    candidates = []
    for h in ("largo", "medio", "corto"):
        for rank, item in enumerate(by_horizon[h], start=1):
            item["rank_in_horizon"] = rank
            candidates.append(item)

    if not candidates:
        logger.warning("Sin candidatos tras el scoring.")
        return []

    # Análisis Claude
    logger.info("Enviando %d candidatos a Claude...", len(candidates))
    analyses = _claude_analysis(candidates)

    # Preparar filas para BD
    now = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    rows = []
    for item in candidates:
        item["claude_analysis"] = analyses.get(item["ticker"], "")
        item["generated_at"]    = now
        rows.append(item)

    save_discoveries(rows)
    logger.info("Descubrimientos guardados: %d resultados", len(rows))
    return rows
