import json
import logging
import math

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from config import ANTHROPIC_API_KEY, MODEL

logger = logging.getLogger("ai_analysis")


def _effective_api_key() -> str:
    """BD > env para ANTHROPIC_API_KEY."""
    try:
        from database import get_setting
        return get_setting("ANTHROPIC_API_KEY") or ANTHROPIC_API_KEY
    except Exception:
        return ANTHROPIC_API_KEY


def _effective_model() -> str:
    """BD > env para MODEL."""
    try:
        from database import get_setting
        return get_setting("MODEL") or MODEL
    except Exception:
        return MODEL


def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=_effective_api_key(), timeout=120)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _call_claude(prompt: str):
    return _get_client().messages.create(
        model=_effective_model(),
        max_tokens=4096,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )


def check_api_health() -> bool:
    """Envía un ping mínimo a Claude. Devuelve True si la API responde."""
    try:
        _get_client().messages.create(
            model=_effective_model(),
            max_tokens=5,
            temperature=0,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True
    except Exception:
        logger.exception("Claude API healthcheck fallido")
        return False

def _format_news(news_by_ticker):
    if not news_by_ticker:
        return ""
    sections = []
    for ticker, headlines in news_by_ticker.items():
        if headlines:
            sections.append(f"*{ticker}*\n" + "\n".join(headlines))
    if not sections:
        return ""
    return "## NOTICIAS RECIENTES\n" + "\n\n".join(sections)

def analyze(portfolio_df, watchlist_df, macro=None, news_by_ticker=None):
    portfolio_str = portfolio_df.to_string(index=False) if not portfolio_df.empty else "Sin posiciones."
    watchlist_str = watchlist_df.to_string(index=False) if not watchlist_df.empty else "Sin activos en watchlist."

    macro_str = ""
    if macro:
        macro_str = f"""
## CONTEXTO MACRO
- S&P 500: €{macro.get('sp500_price', '—')} | YTD (año actual): {macro.get('sp500_ytd', '—')}% | Drawdown 52s: {macro.get('sp500_drawdown', '—')}%
- VIX (volatilidad de mercado): {macro.get('vix', '—')}
- Bono EE.UU. 10 años: {macro.get('treasury_10y', '—')}%
"""

    news_str = _format_news(news_by_ticker)

    prompt = f"""
Eres un analista de inversiones disciplinado, estilo Buffett. Sé breve y estructurado.
IMPORTANTE: El texto se enviará por Telegram. NO uses tablas markdown (|), NO uses ## o ### para cabeceras, NO uses **negrita** con doble asterisco. Usa *negrita* con asterisco simple, guiones para listas y texto plano.
{macro_str}
## CARTERA ACTUAL
{portfolio_str}

## WATCHLIST
{watchlist_str}

{news_str}

Todos los precios están en EUR. Columnas de precio/técnico: price (EUR), drawdown_52w (% caída desde máx anual), \
momentum_3m/6m (% rendimiento), volatility (volatilidad anualizada %), dividend_yield (%), \
trend (mejorando/empeorando), pnl (% vs precio compra en EUR).

Columnas fundamentales: pe_ratio (PER), pb_ratio (P/B), profit_margin (%), roe (%), \
debt_equity (D/E), revenue_growth (% YoY), market_cap_b (capitalización en miles de millones).

Columna horizonte (horizon): plazo de inversión esperado para cada activo.
- *corto* (días – 3 meses): prioriza señales técnicas (RSI, momentum, rebotes). No exijas fundamentales sólidos.
- *medio* (3 – 18 meses): equilibra fundamentales y momentum. Considera catalizadores próximos.
- *largo* (>18 meses): prioriza calidad del negocio, dividendo y ventaja competitiva. Ignora ruido de corto plazo.
Adapta siempre la recomendación al horizonte del activo.

Columnas de consenso de analistas (pueden estar vacías si yfinance no dispone de datos):
- analyst_rec: recomendación media (1=Compra fuerte, 2=Compra, 3=Neutral, 4=Venta, 5=Venta fuerte)
- analyst_target: precio objetivo medio de los analistas en EUR
- analyst_n: número de analistas que cubren el valor
Cuando estén disponibles, compara el precio actual con analyst_target para estimar el potencial y menciona si el consenso respalda o contradice tu análisis.

Responde en este formato exacto:

*CARTERA*
Para cada posición: acción recomendada (mantener/recortar X%/añadir X%), motivo adaptado al horizonte del activo. \
Si hay precio objetivo de analistas, indica el potencial implícito. \
Si recomiendas recortar o añadir, especifica el porcentaje aproximado de la posición actual.

*WATCHLIST — TOP OPORTUNIDADES*
Los 3 activos con mejor relación calidad/precio ahora, teniendo en cuenta su horizonte y el consenso de analistas cuando esté disponible.

*ALERTAS*
Señales de riesgo relevantes (noticias negativas, deterioro de fundamentales, drawdown acelerado, consenso muy negativo) o "Sin alertas." si no hay.
"""

    response = _call_claude(prompt)

    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


# ── Helpers compartidos ────────────────────────────────────────────────────────

def _nan(v) -> bool:
    return isinstance(v, float) and math.isnan(v)


def _call_claude_short(prompt: str, max_tokens: int = 512) -> str:
    """Llama a Claude para tareas cortas sin reintentos. Devuelve '' si falla."""
    try:
        resp = _get_client().messages.create(
            model=_effective_model(),
            max_tokens=max_tokens,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "text":
                return block.text.strip()
    except Exception:
        logger.exception("Error en _call_claude_short")
    return ""


# ── Análisis de ticker individual ─────────────────────────────────────────────

def explain_ticker(ticker: str, notes: str, csv_row: dict, fundamentals: dict) -> str:
    """Evalúa la tesis de inversión y explica el score actual de un ticker."""
    metrics = []
    if csv_row:
        price = csv_row.get("price")
        if price and not _nan(float(price)):
            metrics.append(f"Precio: €{float(price):.2f}")
        dd = csv_row.get("drawdown_52w")
        if dd is not None and not _nan(float(dd)):
            metrics.append(f"Drawdown 52s: {float(dd):.1f}%")
        mom3 = csv_row.get("momentum_3m")
        if mom3 is not None and not _nan(float(mom3)):
            metrics.append(f"Momentum 3m: {float(mom3):.1f}%")
        score = csv_row.get("score")
        if score is not None and not _nan(float(score)):
            metrics.append(f"Score: {float(score):.1f}")
        opp = csv_row.get("opportunity")
        if opp and str(opp) != "nan":
            metrics.append(f"Oportunidad: {opp}")
        horizon = csv_row.get("horizon")
        if horizon and str(horizon) != "nan":
            metrics.append(f"Horizonte: {horizon}")

    fund = []
    for k, label in [("pe_ratio", "PER"), ("pb_ratio", "P/B"), ("roe", "ROE"),
                      ("profit_margin", "Margen neto"), ("debt_equity", "D/E"),
                      ("revenue_growth", "Crec. ingresos")]:
        v = fundamentals.get(k)
        if v is not None and not _nan(float(v)) if isinstance(v, float) else v is not None:
            fund.append(f"{label}: {v}")

    parts = []
    if metrics:
        parts.append("Métricas actuales: " + ", ".join(metrics))
    if fund:
        parts.append("Fundamentales: " + ", ".join(str(f) for f in fund))
    if notes:
        parts.append(f"Tesis del inversor: {notes}")

    data_str = "\n".join(parts) if parts else "Sin datos suficientes."

    prompt = (
        f"Eres un analista de inversiones value. Evalúa la posición "
        f"{ticker} ({fundamentals.get('name', '')}) en 4-5 líneas concisas:\n\n"
        f"{data_str}\n\n"
        "Responde a: 1) ¿El score y métricas justifican mantener/ampliar/reducir? "
        "2) ¿La tesis sigue vigente o hay señales en contra? "
        "Sé directo. Sin introducción. En español."
    )
    return _call_claude_short(prompt, max_tokens=400)


# ── Resumen inteligente de alertas ────────────────────────────────────────────

def summarize_alerts(alerts: list) -> str:
    """Agrupa y contextualiza múltiples alertas disparadas simultáneamente."""
    if len(alerts) < 2:
        return ""
    prompt = (
        "Se han disparado estas alertas de inversión:\n\n"
        + "\n".join(alerts)
        + "\n\nRedacta en 2-3 líneas un resumen contextualizado: "
        "¿hay un patrón común (corrección sectorial, caída macro)? "
        "¿qué acción concreta se recomienda? "
        "Sé directo. Sin introducción. En español."
    )
    return _call_claude_short(prompt, max_tokens=300)


# ── Análisis de operaciones pasadas ───────────────────────────────────────────

def analyze_operations(ops: list, current_prices: dict) -> str:
    """Evalúa el historial de compras/ventas y detecta patrones de comportamiento."""
    if not ops:
        return ""
    lines = []
    for op in ops[:25]:
        op_id, ticker, date, op_type, shares, price, notes = op
        current = current_prices.get(ticker)
        pnl_str = ""
        if current and not _nan(float(current)) and price and not _nan(float(price)):
            if op_type == "buy":
                pnl = (float(current) - float(price)) / float(price) * 100
                pnl_str = f" → P&L actual: {pnl:+.1f}%"
        lines.append(
            f"- {date} {op_type.upper()} {ticker}: {shares:.4g} acc. a €{price:.2f}{pnl_str}"
            + (f" (nota: {notes})" if notes else "")
        )
    prompt = (
        "Eres un analista de inversiones. Evalúa este historial de operaciones:\n\n"
        + "\n".join(lines)
        + "\n\nEn 4-5 líneas:\n"
        "1. ¿Hubo errores de timing (compras cerca de máximos, ventas cerca de mínimos)?\n"
        "2. ¿Qué operaciones resultaron acertadas y por qué?\n"
        "3. ¿Hay algún patrón de comportamiento inversor a mejorar?\n"
        "Sé directo y constructivo. Sin introducción. En español."
    )
    return _call_claude_short(prompt, max_tokens=500)


# ── Sugerencia de rebalanceo ──────────────────────────────────────────────────

def suggest_rebalance(rows: list, total: float) -> str:
    """Justifica con lenguaje natural los ajustes de rebalanceo recomendados."""
    if not rows:
        return ""
    lines = []
    for r in rows:
        diff_str = f"{r['diff']:+.1f}%" if r.get("diff") is not None else "sin objetivo"
        tw_str   = f"{r['target_w']}%" if r.get("target_w") is not None else "—"
        score_str = f"{r['score']:.1f}" if r.get("score") and not _nan(float(r["score"])) else "—"
        lines.append(
            f"- {r['ticker']} ({r['name']}): peso actual {r['current_w']:.1f}%, "
            f"objetivo {tw_str}, desviación {diff_str}, "
            f"horizonte {r.get('horizon') or '—'}, score {score_str}"
        )
    prompt = (
        "Eres un asesor de carteras. Analiza este rebalanceo y da 4-5 recomendaciones concretas. "
        "Considera el horizonte y el score, no solo la desviación del peso objetivo. "
        f"Valor total cartera: €{total:,.0f}\n\n"
        + "\n".join(lines)
        + "\n\nSé breve y directo. Sin introducción. En español."
    )
    return _call_claude_short(prompt, max_tokens=500)


# ── Detección de patrones en noticias ─────────────────────────────────────────

def detect_news_patterns(headlines_by_ticker: dict) -> str:
    """Identifica temas transversales que afectan a varios activos de la cartera."""
    all_headlines = []
    for ticker, items in headlines_by_ticker.items():
        for h in items:
            all_headlines.append(f"[{ticker}] {h}")
    if not all_headlines:
        return ""
    prompt = (
        "Eres un analista financiero macro. Analiza estos titulares de noticias de una cartera "
        "e identifica en 3-4 líneas:\n"
        "1. Temas o tendencias transversales que afectan a varios activos\n"
        "2. Riesgos macro o sectoriales emergentes\n"
        "3. Catalizadores positivos o negativos relevantes\n\n"
        + "\n".join(all_headlines[:30])
        + "\n\nSé conciso. Sin introducción. En español."
    )
    return _call_claude_short(prompt, max_tokens=400)


# ── Resumen corto del informe diario ──────────────────────────────────────────

def summarize_report(report_text: str) -> str:
    """Comprime el informe diario a 3 líneas para Telegram."""
    if not report_text or len(report_text) < 200:
        return report_text
    prompt = (
        "Resume este informe de inversión en EXACTAMENTE 3 líneas en español. "
        "Formato: 1) Estado general de la cartera, 2) Acción más urgente, 3) Principal riesgo o alerta. "
        "Sin saludos, sin títulos, solo las 3 líneas.\n\n"
        + report_text[:3000]
    )
    return _call_claude_short(prompt, max_tokens=200)


# ── Sugerencia de metadata para ticker nuevo ──────────────────────────────────

def suggest_ticker_meta(ticker: str, info: dict) -> dict:
    """Sugiere horizonte y tesis de inversión para un ticker nuevo basándose en sus fundamentales."""
    name        = info.get("longName") or info.get("shortName") or ticker
    sector      = info.get("sector", "")
    country     = info.get("country", "")
    description = (info.get("longBusinessSummary") or "")[:400]
    pe  = info.get("trailingPE")
    roe = info.get("returnOnEquity")
    div = info.get("dividendYield")

    data_parts = [f"Empresa: {name}", f"Sector: {sector}", f"País: {country}"]
    if pe  and not _nan(float(pe)):  data_parts.append(f"PER: {float(pe):.1f}")
    if roe and not _nan(float(roe)): data_parts.append(f"ROE: {float(roe)*100:.1f}%")
    if div and not _nan(float(div)): data_parts.append(f"Dividend Yield: {float(div)*100:.1f}%")
    if description:
        data_parts.append(f"Descripción: {description}")

    prompt = (
        f"Para el activo {ticker} con estos datos:\n"
        + "\n".join(data_parts)
        + "\n\nResponde SOLO con un JSON válido (sin markdown ni explicaciones) con dos campos:\n"
        '{"horizon": "largo|medio|corto", "notes": "tesis de inversión en 1-2 frases"}\n'
        "Criterios de horizonte: largo=negocio consolidado con dividendo o ventaja competitiva duradera; "
        "corto=growth acelerado o cíclico; medio=resto."
    )
    try:
        text = _call_claude_short(prompt, max_tokens=200)
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
            horizon = data.get("horizon", "medio")
            if horizon not in ("largo", "medio", "corto"):
                horizon = "medio"
            notes = str(data.get("notes", ""))[:500]
            return {"horizon": horizon, "notes": notes}
    except Exception:
        logger.exception("Error en suggest_ticker_meta")
    return {"horizon": "medio", "notes": ""}
