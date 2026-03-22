FROM python:3.12-slim

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
RUN chmod +x /entrypoint.sh

USER appuser

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "scheduler.py"]
