import json
import logging
import math
import re

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from config import ANTHROPIC_API_KEY, MODEL

logger = logging.getLogger("ai_analysis")

# ISO 27001 A.14.2: limpiar texto de usuario antes de incluirlo en prompts Claude
def _safe_for_prompt(text: str, max_len: int = 500) -> str:
    """Elimina caracteres de control para prevenir prompt injection."""
    if not text:
        return ""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(text).strip())[:max_len]


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
def _call_claude(prompt: str, use_cache: bool = False):
    content = [{"type": "text", "text": prompt}]
    if use_cache:
        content[-1]["cache_control"] = {"type": "ephemeral"}
    return _get_client().messages.create(
        model=_effective_model(),
        max_tokens=4096,
        temperature=0,
        messages=[{"role": "user", "content": content}],
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

def _fmt(v, suffix="", decimals=2, na="—"):
    """Formatea un valor numérico de forma segura."""
    if v is None:
        return na
    try:
        f = float(v)
        if math.isnan(f):
            return na
        return f"{f:.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return na


_HORIZON_FOCUS = {
    "corto": (
        "CORTO PLAZO (días–3m): Prioriza señales técnicas. "
        "Analiza RSI, momentum 3m y drawdown desde máximo. "
        "No penalices por fundamentales débiles."
    ),
    "medio": (
        "MEDIO PLAZO (3–18m): Equilibra técnico y fundamental. "
        "Considera momentum, score, PER y ROE. "
        "Identifica catalizadores próximos."
    ),
    "largo": (
        "LARGO PLAZO (>18m): Prioriza calidad del negocio, dividendo y ventaja competitiva. "
        "Destaca ROE, margen, deuda y yield. "
        "Ignora ruido de corto plazo; busca si el precio está atractivo a largo."
    ),
}


def _build_position_block(row) -> str:
    """Construye un bloque de texto estructurado para una fila del DataFrame."""
    ticker   = row.get("ticker", "?")
    name     = row.get("name", ticker)
    horizon  = str(row.get("horizon") or "").strip()
    if not horizon or horizon == "nan":
        horizon = None

    horizon_label = {
        "corto": "⚡ Corto plazo",
        "medio": "📈 Medio plazo",
        "largo": "🏦 Largo plazo",
    }.get(horizon, "❓ No definido")

    focus = _HORIZON_FOCUS.get(horizon, "Sin horizonte definido — usar criterio equilibrado.")

    price   = _fmt(row.get("price"), suffix=" EUR")
    dd      = _fmt(row.get("drawdown_52w"), suffix="%")
    mom3    = _fmt(row.get("momentum_3m"), suffix="%")
    mom6    = _fmt(row.get("momentum_6m"), suffix="%")
    vol     = _fmt(row.get("volatility"), suffix="%")
    rsi     = _fmt(row.get("rsi"), decimals=1)
    div     = _fmt(row.get("dividend_yield"), suffix="%")
    score   = _fmt(row.get("score"), decimals=1)
    opp     = str(row.get("opportunity") or "—")
    pnl     = _fmt(row.get("pnl"), suffix="%")
    trend   = str(row.get("trend") or "—")
    block   = str(row.get("block") or "—")
    region  = str(row.get("region") or "—")

    per     = _fmt(row.get("pe_ratio"), decimals=1)
    pb      = _fmt(row.get("pb_ratio"), decimals=2)
    roe     = _fmt(row.get("roe"), suffix="%")
    margin  = _fmt(row.get("profit_margin"), suffix="%")
    de      = _fmt(row.get("debt_equity"), decimals=2)
    rev_g   = _fmt(row.get("revenue_growth"), suffix="%")
    cap     = _fmt(row.get("market_cap_b"), decimals=1, suffix=" Bn€")

    a_rec    = _fmt(row.get("analyst_rec"), decimals=1)
    a_target = _fmt(row.get("analyst_target"), suffix=" EUR")
    a_n      = row.get("analyst_n")
    analyst_str = ""
    if a_rec != "—" or a_target != "—":
        analyst_str = f"  Analistas: rec={a_rec}/5 · target={a_target}"
        if a_n and not math.isnan(float(a_n)) if isinstance(a_n, float) else a_n:
            analyst_str += f" ({int(float(a_n))} analistas)"

    lines = [
        f"[{ticker}] {name} | {block} · {region} | {horizon_label}",
        f"  Enfoque: {focus}",
        f"  Precio: {price} | Drawdown 52s: {dd} | Momentum 3m: {mom3} | Momentum 6m: {mom6}",
        f"  RSI(14): {rsi} | Volatilidad: {vol} | Dividendo: {div} | Tendencia: {trend}",
        f"  Score: {score} | Oportunidad: {opp}" + (f" | P&L vs coste (EUR): {pnl}" if pnl != "—" else ""),
        f"  PER: {per} | P/B: {pb} | ROE: {roe} | Margen: {margin} | D/E: {de} | Crec.Ing: {rev_g} | Cap: {cap}",
    ]
    if analyst_str:
        lines.append(analyst_str)

    return "\n".join(lines)


def analyze(portfolio_df, watchlist_df, macro=None, news_by_ticker=None):
    macro_str = ""
    if macro:
        macro_str = (
            "*CONTEXTO MACRO*\n"
            f"- S&P 500: €{macro.get('sp500_price', '—')} | "
            f"YTD: {macro.get('sp500_ytd', '—')}% | "
            f"Drawdown 52s: {macro.get('sp500_drawdown', '—')}%\n"
            f"- VIX: {macro.get('vix', '—')}\n"
            f"- Bono EE.UU. 10 años: {macro.get('treasury_10y', '—')}%\n"
        )

    # Valor total de cartera desde el último snapshot guardado en BD
    portfolio_total_str = ""
    try:
        from database import get_portfolio_value_history
        history = get_portfolio_value_history(days=3)
        if history:
            total_eur = history[-1][1]  # (date, total_eur, positions_count)
            if total_eur and total_eur > 0:
                portfolio_total_str = f"Valor total cartera (último snapshot, EUR): €{total_eur:,.0f}\n"
    except Exception:
        pass

    # Portfolio ordenado por horizonte: corto → medio → largo → sin definir
    _HORIZON_ORDER = {"corto": 0, "medio": 1, "largo": 2}
    portfolio_blocks = []
    if not portfolio_df.empty:
        sorted_port = portfolio_df.copy()
        if "horizon" in sorted_port.columns:
            sorted_port["_h_ord"] = sorted_port["horizon"].apply(
                lambda h: _HORIZON_ORDER.get(str(h).strip() if h and str(h) != "nan" else "", 3)
            )
            sorted_port = sorted_port.sort_values("_h_ord")
        for d in sorted_port.to_dict("records"):
            portfolio_blocks.append(_build_position_block(d))
    portfolio_str = "\n\n".join(portfolio_blocks) if portfolio_blocks else "Sin posiciones."

    watchlist_blocks = []
    if not watchlist_df.empty:
        # Watchlist ordenada por score descendente para que Claude vea las mejores oportunidades primero
        sorted_wl = watchlist_df.sort_values("score", ascending=False) if "score" in watchlist_df.columns else watchlist_df
        for d in sorted_wl.to_dict("records"):
            watchlist_blocks.append(_build_position_block(d))
    watchlist_str = "\n\n".join(watchlist_blocks) if watchlist_blocks else "Sin activos en watchlist."

    news_str = _format_news(news_by_ticker)

    prompt = f"""Eres un analista de inversiones disciplinado. Sé breve, concreto y estructurado.
IMPORTANTE: Texto para Telegram. NO uses tablas markdown (|), NO uses ## o ###, NO uses **negrita**. Usa *negrita* con asterisco simple, guiones para listas.

{macro_str}{portfolio_total_str}
*INSTRUCCIONES DE ANÁLISIS POR HORIZONTE*
Para cada activo, el campo "Enfoque" indica la metodología a aplicar:
- Horizonte CORTO (⚡): Prioriza RSI, momentum y drawdown. No exijas fundamentales sólidos. Busca rebotes técnicos.
- Horizonte MEDIO (📈): Equilibra técnico y fundamental. Considera momentum, PER y ROE. Identifica catalizadores.
- Horizonte LARGO (🏦): Prioriza calidad del negocio, dividendo y ventaja competitiva. ROE, margen, D/E. Ignora ruido diario.
- Sin horizonte (❓): Aplica criterio equilibrado, menciona que sería útil definir el horizonte.

*CARTERA ACTUAL* (ordenada por horizonte: corto → medio → largo)
{portfolio_str}

*WATCHLIST* (ordenada por score, mayor primero)
{watchlist_str}

{news_str}

Responde en este formato exacto (sin añadir secciones extra):

*RESUMEN EJECUTIVO*
1-2 frases: estado general de la cartera hoy en relación al contexto macro.

*CARTERA*
Para cada posición: acción recomendada (mantener / recortar X% / añadir X%), motivo adaptado estrictamente al horizonte del activo. Si hay precio objetivo de analistas, indica el potencial implícito (precio actual vs target). Máximo 2 líneas por posición.

*WATCHLIST — TOP OPORTUNIDADES*
Los 3 activos con mejor relación calidad/precio considerando su horizonte específico. Para cada uno: por qué es atractivo ahora y qué nivel/condición lo haría más interesante aún. Si el consenso de analistas está disponible, menciónalo.

*ALERTAS*
Señales de riesgo relevantes (noticias negativas, deterioro de fundamentales, drawdown acelerado, RSI sobrecomprado en corto plazo, consenso negativo) o "Sin alertas." si no hay ninguna.
"""

    response = _call_claude(prompt, use_cache=True)

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
        parts.append(f"Tesis del inversor: {_safe_for_prompt(notes)}")

    data_str = "\n".join(parts) if parts else "Sin datos suficientes."

    horizon = csv_row.get("horizon") if csv_row else None
    horizon_inst = _HORIZON_FOCUS.get(str(horizon).strip() if horizon and str(horizon) != "nan" else "", "")
    horizon_line = f"\nHorizonte de inversión configurado: {horizon_inst}" if horizon_inst else ""

    prompt = (
        f"Eres un analista de inversiones. Evalúa la posición "
        f"{ticker} ({fundamentals.get('name', '')}) en 4-5 líneas concisas.\n"
        f"Todos los precios están en EUR.{horizon_line}\n\n"
        f"{data_str}\n\n"
        "Responde a: 1) ¿El score y métricas justifican mantener/ampliar/reducir? "
        "2) ¿La tesis sigue vigente o hay señales en contra? "
        "Adapta el análisis al horizonte indicado. Sé directo. Sin introducción. En español."
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
        # get_operations devuelve: id, ticker, date, type, shares, price_eur, notes, commission_eur
        op_id, ticker, date, op_type, shares, price, notes = op[:7]
        commission = op[7] if len(op) > 7 else 1.0
        current = current_prices.get(ticker)
        pnl_str = ""
        if current and not _nan(float(current)) and price and not _nan(float(price)):
            if op_type == "buy":
                net_cost = float(shares) * float(price) + float(commission or 1.0)
                net_current = float(shares) * float(current)
                pnl = (net_current - net_cost) / net_cost * 100
                pnl_str = f" → P&L neto (comisión incl.): {pnl:+.1f}%"
        commission_str = f" [comisión: €{float(commission or 1.0):.2f}]" if commission else ""
        lines.append(
            f"- {date} {op_type.upper()} {ticker}: {shares:.4g} acc. a €{price:.2f}{commission_str}{pnl_str}"
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


def suggest_operation_note(ticker: str, op_type: str, price_eur: float, date: str) -> str:
    """Sugiere una nota breve para registrar en una operación de compra/venta."""
    action = "compra" if op_type == "buy" else "venta"
    prompt = (
        f"Eres un asistente de inversión. Sugiere una nota concisa (máx. 80 caracteres) para "
        f"registrar en el historial de operaciones una {action} de {ticker} a €{price_eur:.2f} "
        f"el {date}. La nota debe resumir el motivo habitual de esta operación de forma neutra "
        f"y profesional. Responde solo con la nota, sin comillas ni explicaciones adicionales."
    )
    result = _call_claude_short(prompt, max_tokens=100)
    return result[:500] if result else ""
