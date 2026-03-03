from openai import OpenAI
from config import OPENAI_API_KEY, MODEL

client = OpenAI(api_key=OPENAI_API_KEY)

def analyze(summary):
    prompt = f"""
Eres analista disciplinado estilo Buffett.
Analiza esta cartera y watchlist:

{summary}

Devuelve:
- Recomendaciones cartera
- Mejores oportunidades watchlist
Breve y estructurado.
"""
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.choices[0].message.content
