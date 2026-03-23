# ISO 27001 A.12.6: imagen base pineada a versión específica para builds reproducibles
FROM python:3.12.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --root-user-action=ignore --upgrade pip \
 && pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY . .

# Descargar Alpine.js 3 para servir localmente (elimina dependencia de CDN externo)
RUN mkdir -p static \
 && curl -fsSL https://unpkg.com/alpinejs@3.14.9/dist/cdn.min.js -o static/alpine.min.js

RUN useradd -m appuser && chown -R appuser /app

COPY entrypoint.sh /entrypoint.sh
# chmod 555: ejecutable pero no modificable por ningún usuario (ISO 27001 A.5.7)
RUN chmod 555 /entrypoint.sh

USER appuser

# ISO 27001 A.12.1 — disponibilidad: el orquestador detecta el proceso caído automáticamente
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import sqlite3, sys; conn = sqlite3.connect('/app/data/radar.db', timeout=5); conn.execute('SELECT 1'); conn.close()" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "scheduler.py"]
