# CLAUDE.md — Market Radar AI

## Descripción general

Bot de Telegram para monitoreo de cartera e inversiones. Descarga datos de mercado via yfinance, calcula métricas técnicas y fundamentales, puntúa oportunidades de inversión y genera análisis diarios con Claude (Anthropic). Incluye dashboard web opcional con FastAPI.

## Stack tecnológico

- **Python 3.12**
- **Telegram**: `python-telegram-bot[job-queue]` 20.7 con APScheduler para jobs periódicos
- **Datos financieros**: `yfinance` — precios históricos, fundamentales, dividendos, noticias
- **IA**: `anthropic` SDK — Claude para análisis de informes y traducción de titulares
- **Web**: `FastAPI` + `uvicorn` + `Jinja2` — dashboard web opcional en puerto 8589
- **Base de datos**: SQLite con WAL mode (`data/radar.db`)
- **Config**: `tickers.yaml` (PyYAML) para cartera y watchlist
- **Visualización**: `matplotlib` con backend Agg (sin display), tema oscuro
- **Despliegue**: Docker + Docker Compose, publicado en GHCR, multi-arquitectura (amd64 + arm64)

## Estructura del proyecto

```
bot.py              # Bot Telegram principal: comandos, jobs APScheduler, gráficos
scheduler.py        # Ejecución standalone (sin bot, útil con cron externo)
generate_csv.py     # Pipeline de datos: descarga, cálculo de métricas, CSV
fetch_data.py       # Wrappers yfinance: precios, dividendos, fundamentales, FX, noticias
scoring.py          # Algoritmo de puntuación multi-factor (6 factores → score)
ai_analysis.py      # Integración Claude: genera el análisis diario
web.py              # Dashboard FastAPI: reportes, posiciones, alertas, rebalanceo
database.py         # CRUD SQLite: portfolio, price_history, price_alerts, reports
config.py           # Parsing y validación de variables de entorno
tickers.yaml        # Cartera y watchlist del usuario (editado via comandos Telegram)
requirements.txt    # Dependencias Python
Dockerfile          # python:3.12-slim, usuario no-root appuser
docker-compose.yml  # Servicios: init, market-radar (bot), market-radar-web
.env.example        # Plantilla de variables de entorno requeridas
templates/          # Plantillas Jinja2 HTML para el dashboard web
```

## Variables de entorno

**Requeridas:**
- `ANTHROPIC_API_KEY` — clave Claude (formato `sk-ant-...`)
- `TELEGRAM_BOT_TOKEN` — token del bot (@BotFather)
- `TELEGRAM_CHAT_ID` — tu ID de Telegram (único usuario autorizado)

**Opcionales:**
- `MODEL` — modelo Claude a usar (default: `claude-haiku-4-5-20251001`)
- `REPORT_HOUR` — hora del informe diario en formato 0-23 (default: `8`)
- `TIMEZONE` — zona horaria IANA (default: `Europe/Madrid`)
- `WEB_PASSWORD` — contraseña del dashboard web (si está vacío, sin autenticación)
- `WEB_PORT` — puerto del dashboard (default: `8589`)

## Cómo ejecutar

```bash
# Con Docker (recomendado)
cp .env.example .env
# editar .env con las credenciales
docker compose up -d

# Sin Docker (desarrollo)
pip install -r requirements.txt
python bot.py           # Bot completo con Telegram
python scheduler.py     # Solo pipeline de análisis (standalone)
uvicorn web:app --host 0.0.0.0 --port 8589  # Solo dashboard web
```

## Base de datos SQLite

Tablas en `data/radar.db`:
- **`portfolio`** — `ticker PK`, `shares`, `avg_price` (en EUR)
- **`price_history`** — snapshots diarios con métricas: `price`, `drawdown_52w`, `momentum_3m/6m`, `volatility`, `dividend_yield`, `score`, `opportunity`; constraint UNIQUE en `(ticker, date)`
- **`price_alerts`** — alertas: `ticker`, `target_price`, `direction` (up/down), `active`
- **`reports`** — informes guardados: `date`, `content`

## Algoritmo de scoring (scoring.py)

Puntuación 0-100 con 6 factores ponderados:
- Drawdown 52w: **30%** (señal contraria — drawdown alto = más oportunidad)
- Momentum 3m: **15%**
- Volatilidad: **15%**
- Dividendo: **15%**
- ROE: **15%**
- P/E ratio: **10%**

Clasificación: `ALTA` (>15), `MEDIA` (>8), `BAJA` (≤8)

## Conversión FX a EUR (fetch_data.py)

- Cache en memoria para evitar llamadas repetidas
- Fallback a tasa 1.0 si yfinance falla
- Guards para `None` y `NaN` antes de convertir
- Tickers europeos usan sufijos yfinance (ej. `OR.PA` para Euronext Paris)

## Flujo del informe diario (08:00 por defecto)

1. `fetch_data.get_macro_context()` → S&P500, VIX, bono 10Y
2. `generate_csv.generate()` → descarga 5 años de histórico, convierte a EUR
3. `scoring.score_watchlist()` → calcula score para cartera y watchlist
4. `database.save_snapshot()` → guarda métricas diarias en SQLite
5. `fetch_data.get_news()` → titulares recientes traducidos con Claude
6. `ai_analysis.analyze()` → análisis completo con Claude
7. `database.save_report()` → guarda informe en SQLite
8. Bot envía el informe al Telegram configurado

## Comandos Telegram disponibles

| Comando | Descripción |
|---------|-------------|
| `/reporte` | Genera informe ahora |
| `/cartera` | Vista de la cartera |
| `/watchlist` | Watchlist ordenada por score |
| `/rebalanceo` | Pesos actuales vs. objetivo |
| `/grafico <ticker>` | Gráfico de precio |
| `/historial <ticker>` | Evolución drawdown 30 días |
| `/fundamentos <ticker>` | Fundamentales |
| `/posicion <ticker> <shares> <precio_eur>` | Registrar posición |
| `/eliminar_posicion <ticker>` | Eliminar posición |
| `/alerta <ticker> <precio>` | Crear alerta de precio |
| `/mis_alertas` | Listar alertas activas |
| `/borrar_alerta <id>` | Eliminar alerta |
| `/agregar <categoria> <ticker> <nombre> <sector> <region>` | Añadir ticker |
| `/eliminar <ticker>` | Eliminar ticker |

## Dashboard web (web.py)

FastAPI app con autenticación por cookie de sesión. Todas las rutas verifican `_is_auth(session)` y redirigen a `/login` si no está autenticado.

**Rutas disponibles:**

| Ruta | Método | Descripción |
|------|--------|-------------|
| `/` | GET | Dashboard principal: cartera con PnL, watchlist por score, último informe |
| `/rebalanceo` | GET | Pesos actuales vs. objetivo con acción recomendada (OK / Recortar / Añadir) |
| `/noticias` | GET | Titulares recientes por ticker, traducidos con Claude |
| `/ticker/{ticker}` | GET | Detalle de un ticker: fundamentales, noticias, historial drawdown |
| `/tickers` | GET | Gestión de cartera y watchlist |
| `/posiciones` | GET | Lista de posiciones con PnL calculado |
| `/alertas` | GET | Alertas activas; dropdown con tickers del CSV |
| `/reportes` | GET | Últimos 10 informes Claude |
| `/generar-reporte` | POST | Lanza pipeline completo en thread pool, redirige a `/` |
| `/tickers/add` | POST | Añade ticker a tickers.yaml |
| `/tickers/delete` | POST | Elimina ticker de tickers.yaml |
| `/posiciones/add` | POST | Registra posición (shares + avg_price EUR) |
| `/posiciones/delete` | POST | Elimina posición |
| `/alertas/add` | POST | Crea alerta; infiere dirección (above/below) del precio actual |
| `/alertas/delete` | POST | Desactiva alerta por ID |
| `/chart/precio/{ticker}` | GET | PNG del precio último año (tema oscuro) |
| `/chart/historial/{ticker}` | GET | PNG del drawdown histórico 30 días (tema oscuro) |
| `/login` | GET/POST | Formulario de login |
| `/logout` | GET | Borra cookie de sesión |

**Filtros Jinja2 registrados:**
- `eur` — formatea como `€1,234.56`; devuelve `—` para None/NaN
- `pct` — formatea como `+1.5%`
- `num` — formatea con 2 decimales
- `dd_class` — CSS class según severidad del drawdown (`text-negative`, `text-warning`, etc.)
- `pnl_class` — CSS class positivo/negativo
- `opp_class` — badge CSS según clasificación ALTA/MEDIA/BAJA
- `tg` — convierte formato Markdown Telegram (`*bold*`, `_italic_`, `` `code` ``) a HTML

**Autenticación:**
- Si `WEB_PASSWORD` está vacío, no hay autenticación (todas las sesiones son válidas)
- Si está configurado, se genera un `SESSION_TOKEN` aleatorio al arrancar (no persiste entre reinicios)
- Cookie `session` con `httponly=True`, `samesite=lax`, expira en 30 días

**Generación de gráficos:**
- `_make_price_chart` — precio de cierre 252 días, marca máximo 52 semanas y precio actual, área sombreada en rojo entre precio actual y máximo
- `_make_history_chart` — drawdown desde máximo 52 semanas (últimos 30 días del radar), área roja bajo cero
- Caché HTTP de 5 min (`Cache-Control: public, max-age=300`)
- Las operaciones yfinance se ejecutan en `_executor` (ThreadPoolExecutor, 4 workers) para no bloquear el event loop

## Notas de implementación

- Los gráficos usan tema oscuro (background `#161b22`), backend Agg (sin display)
- La traducción de titulares usa Claude con cache en memoria; fallback a inglés
- Los mensajes Telegram largos se dividen automáticamente para evitar errores de Markdown
- El healthcheck Docker verifica conectividad SQLite cada 60s
- No hay suite de tests automatizados; se valida manualmente via Telegram o `scheduler.py`
- `generate_csv.py` continúa si falla un ticker individual (recopila errores al final)
- El dashboard web usa un executor de hilos para no bloquear al generar informes

## CI/CD

GitHub Actions en `.github/workflows/docker-publish.yml`:
- Trigger: push a `main` o `workflow_dispatch`
- Build multi-arquitectura con QEMU (amd64 + arm64)
- Publica en `ghcr.io/dazanestor/market-radar-ai:latest` + tag SHA
