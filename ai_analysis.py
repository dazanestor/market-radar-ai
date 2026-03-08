import anthropic
from config import ANTHROPIC_API_KEY, MODEL

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def _format_news(news_by_ticker):
    if not news_by_ticker:
        return ""
    sections = []
    for ticker, headlines in news_by_ticker.items():
        if headlines:
            sections.append(f"**{ticker}**\n" + "\n".join(headlines))
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
- S&P 500: ${macro.get('sp500_price', '—')} | YTD: {macro.get('sp500_ytd', '—')}% | Drawdown: {macro.get('sp500_drawdown', '—')}%
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

Responde en este formato exacto:

**CARTERA**
Para cada posición: acción recomendada (mantener/recortar X%/añadir X%), motivo basado en fundamentales + técnico. Si recomiendas recortar o añadir, especifica el porcentaje aproximado de la posición actual.

**WATCHLIST — TOP OPORTUNIDADES**
Los 3 activos con mejor relación calidad/precio ahora y por qué.

**ALERTAS**
Señales de riesgo relevantes (noticias negativas, deterioro de fundamentales, drawdown acelerado) o "Sin alertas." si no hay.
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        temperature=0,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text
