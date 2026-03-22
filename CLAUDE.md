# CLAUDE.md — Market Radar AI

## Descripción general

Herramienta de monitoreo de cartera e inversiones. Descarga datos de mercado via yfinance, calcula métricas técnicas y fundamentales, puntúa oportunidades de inversión y genera análisis diarios con Claude (Anthropic). Dashboard web con FastAPI + notificaciones via Web Push (PWA).

## Stack tecnológico

- **Python 3.12**
- **Scheduler**: `APScheduler` 3.10 — `BlockingScheduler` con `CronTrigger` e `IntervalTrigger` para jobs periódicos en `scheduler.py`
- **Datos financieros**: `yfinance` — precios históricos, fundamentales, dividendos, noticias
- **IA**: `anthropic` SDK — Claude para análisis de informes y traducción de titulares
- **Web**: `FastAPI` + `uvicorn` + `Jinja2` — dashboard web opcional en puerto 8589
- **Base de datos**: SQLite con WAL mode (`data/radar.db`)
- **Config**: tabla `tickers` en SQLite — cartera y watchlist; `tickers.yaml` solo se usa para migración inicial (one-time) si la tabla está vacía
- **Visualización**: `matplotlib` con backend Agg (sin display), tema oscuro
- **Optimización de cartera**: `scipy` (SLSQP) — Mínima Varianza, Máximo Sharpe y Paridad de Riesgo con frontera eficiente
- **Despliegue**: Docker + Docker Compose, publicado en GHCR, multi-arquitectura (amd64 + arm64)
- **Web Push PWA**: `push_utils.py` — notificaciones push al navegador sin dependencias externas (VAPID RFC 8292 + cifrado RFC 8291 con `cryptography` y `requests` ya disponibles)

## Estructura del proyecto

```
scheduler.py        # Servicio de jobs periódicos: reporte diario, alertas, ex-div, earnings, vacuum
generate_csv.py     # Pipeline de datos: descarga, cálculo de métricas, guarda snapshots en BD (no escribe CSV)
fetch_data.py       # Wrappers yfinance: precios, dividendos, fundamentales, FX, noticias
scoring.py          # Algoritmo de puntuación multi-factor (7 factores → score, pesos por horizonte)
ai_analysis.py      # Integración Claude: genera el análisis diario
web.py              # Dashboard FastAPI: reportes, posiciones, alertas, rebalanceo
push_utils.py       # Web Push VAPID: genera claves, cifra payload, envía push al navegador
database.py         # CRUD SQLite: portfolio, tickers, price_history, price_alerts, reports, operations
config.py           # Parsing y validación de variables de entorno
tickers.yaml        # Solo para migración inicial; se puede eliminar una vez arrancado por primera vez
requirements.txt    # Dependencias Python
Dockerfile          # python:3.12-slim, usuario no-root appuser
docker-compose.yml  # Servicios: init, market-radar (scheduler), market-radar-web
.env.example        # Plantilla de variables de entorno requeridas
templates/          # Plantillas Jinja2 HTML para el dashboard web
```

## Variables de entorno

**Requeridas:**
- `ANTHROPIC_API_KEY` — clave Claude (formato `sk-ant-...`)

**Opcionales:**
- `MODEL` — modelo Claude a usar (default: `claude-haiku-4-5-20251001`)
- `REPORT_HOUR` — hora del informe diario en formato 0-23 (default: `8`)
- `TIMEZONE` — zona horaria IANA (default: `Europe/Madrid`)
- `WEB_PASSWORD` — contraseña del dashboard web (si está vacío, sin autenticación)
- `WEB_PORT` — puerto del dashboard (default: `8589`)
- `MPLCONFIGDIR` — directorio de caché de matplotlib; en Docker apunta a `/app/output/.matplotlib` para evitar fallos de permisos con el usuario `appuser`

## Cómo ejecutar

```bash
# Con Docker (recomendado)
cp .env.example .env
# editar .env con las credenciales
docker compose up -d

# Sin Docker (desarrollo)
pip install -r requirements.txt
python scheduler.py     # Servicio de jobs periódicos (reporte, alertas, etc.)
uvicorn web:app --host 0.0.0.0 --port 8589  # Dashboard web
```

## Base de datos SQLite

Tablas en `data/radar.db`:
- **`portfolio`** — `ticker PK`, `shares`, `avg_price` (en EUR)
- **`price_history`** — snapshots diarios con métricas: `price`, `drawdown_52w`, `momentum_3m/6m`, `volatility`, `dividend_yield`, `rsi`, `score`, `opportunity`; constraint UNIQUE en `(ticker, date)`
- **`price_alerts`** — alertas: `ticker`, `target_price`, `direction` (above/below), `condition_type` (price/drawdown/score/stoploss_pct), `condition_value`, `active`
- **`alert_history`** — historial de alertas disparadas: `ticker`, `target_price`, `direction`, `condition_type`, `condition_value`, `triggered_at`, `price_at_trigger`; campo `notified` para reenvío tras caída del bot
- **`reports`** — informes guardados: `date`, `content`
- **`news_cache`** — caché de traducciones de titulares: `headline_hash PK`, `translation`, `fetched_at` (TTL 24h)
- **`tr_cache`** — caché clave-valor para datos Trade Republic (cash_eur, transacciones, posiciones no mapeadas): `key PK`, `value`, `updated`
- **`push_subscriptions`** — suscripciones Web Push: `endpoint UNIQUE`, `p256dh`, `auth`, `user_agent`, `created`
- **`operations`** — historial de operaciones: `ticker`, `date`, `type` (buy/sell), `shares`, `price_eur`, `notes`
- **`portfolio_value`** — valor total diario de la cartera: `date UNIQUE`, `total_eur`, `positions_count`
- **Backups automáticos**: el servicio `backup` en docker-compose copia `radar.db` a `data/backups/radar_YYYYMMDD.db` cada 24h, conservando los últimos 7 snapshots.

## Algoritmo de scoring (scoring.py)

Tres conjuntos de pesos según el horizonte de inversión del ticker. RSI(14) es el 7º factor:

| Factor | Largo plazo | Medio plazo | Corto plazo |
|--------|------------|------------|------------|
| Drawdown 52w | 20% | 25% | 25% |
| Momentum 3m | 5% | 15% | 20% |
| Volatilidad | 15% | 10% | 5% |
| Dividendo | 20% | 10% | 0% |
| ROE | 25% | 15% | 0% |
| PER | 15% | 15% | 0% |
| RSI(14) | 0% | 10% | **50%** |

Clasificación: `ALTA` (>15), `MEDIA` (>8), `BAJA` (≤8), `—` (sin datos)

**Horizontes temporales:**
- `corto`: días – 3 meses. Señales técnicas (RSI, momentum, rebotes).
- `medio`: 3 meses – 18 meses. Mix fundamentales + momentum.
- `largo`: 18 meses – varios años. Calidad del negocio + dividendo.

`suggest_horizon(roe, pe, div, vol, mom3m)` infiere el horizonte óptimo automáticamente.
`HORIZON_META` expone etiquetas, rangos y descripciones para UI y bot.

## Algoritmo de optimización de cartera (web.py)

`_compute_optimization(df_portfolio, positions_map)` calcula tres carteras óptimas usando la teoría moderna de carteras (Markowitz) con scipy SLSQP.

### Retornos esperados multi-factor (horizon-aware)

Los retornos esperados de cada activo combinan 5 fuentes con pesos que varían según el horizonte del ticker:

| Factor | Corto plazo | Medio plazo | Largo plazo |
|--------|------------|------------|------------|
| Retorno histórico 1 año | 35% | 40% | 40% |
| Score del radar | 20% | 20% | 20% |
| Precio objetivo analistas | 10% | 15% | 20% |
| Momentum 3m/6m | 25% | 15% | 5% |
| Calidad fundamental (ROE, PER) | 10% | 10% | 15% |

### Tres carteras óptimas

| Cartera | Objetivo | Restricciones |
|---------|----------|---------------|
| **Mínima Varianza** | `min w'Σw` | Suma=1, sin cortos, 1%≤wᵢ≤max_w |
| **Máximo Sharpe** | `max (μ'w − Rf) / √(w'Σw)` | Ídem; Rf=3% anual |
| **Paridad de Riesgo** | `min Σᵢ(RCᵢ − 1/n)²` donde `RCᵢ = wᵢ·(Σw)ᵢ / w'Σw` | Suma=1, 1%≤wᵢ≤max_w |

Restricción de concentración: peso máximo = `min(40%, max(3/n, 10%))` donde `n` es el número de activos.

### Frontera eficiente

40 puntos entre el retorno de la cartera de mínima varianza y el retorno máximo individual. Cada punto es una optimización de varianza mínima con restricción de retorno igual a target. El gráfico incluye la Capital Market Line desde Rf hasta Máximo Sharpe.

### Caché de optimización

- `_opt_cache: dict` + `_opt_cache_lock: threading.RLock()` — TTL de 5 minutos (`_OPT_CACHE_TTL = 300.0`)
- La función `_get_opt_cached(df_portfolio, positions_map)` es usada tanto por `/optimizacion` como por `/chart/frontera-eficiente` para evitar doble cómputo
- `_invalidate_csv_cache()` también invalida `_opt_cache` para que el siguiente reporte regenere la optimización con datos frescos

## Conversión FX a EUR (fetch_data.py)

- Cache en memoria para evitar llamadas repetidas
- Fallback a tasa 1.0 si yfinance falla
- Guards para `None` y `NaN` antes de convertir
- Tickers europeos usan sufijos yfinance (ej. `OR.PA` para Euronext Paris)

## Flujo del informe diario (08:00 por defecto)

1. `fetch_data.get_macro_context()` → S&P500, VIX, bono 10Y
2. `generate_csv.generate()` → descarga 5 años de histórico, convierte a EUR; incluye consenso de analistas (`analyst_rec`, `analyst_target`, `analyst_n`) y horizonte por ticker (`horizon`) en el CSV
3. `scoring.score_watchlist()` → calcula score para cartera y watchlist
4. `database.save_snapshot()` → guarda métricas diarias en SQLite
5. `database.save_portfolio_value()` → guarda valor total de cartera para gráfico de evolución
6. `fetch_data.get_news()` → titulares recientes traducidos con Claude
7. `ai_analysis.analyze()` → análisis completo con Claude
8. `database.save_report()` → guarda informe en SQLite
9. Web Push al navegador via `push_utils.send_push_to_all()`

## Scheduler (scheduler.py)

Servicio de larga ejecución con APScheduler `BlockingScheduler`. Todas las notificaciones van via Web Push al navegador (sin Telegram).

| Job | Cuándo | Qué hace |
|-----|--------|----------|
| `job_daily_report` | `REPORT_HOUR`:00 diario | Pipeline completo + Web Push con resumen |
| `job_check_exdividend` | 07:00 diario | Web Push si algún ticker tiene ex-dividend en ≤3 días |
| `job_check_earnings` | 07:00 diario | Web Push si algún ticker tiene earnings en ≤7 días |
| `job_check_price_alerts` | Cada hora | Dispara alertas precio/drawdown/score/stoploss; Web Push |
| `job_check_sector_concentration` | Cada 24h | Web Push si algún sector supera el umbral (default 40%) |
| `job_replay_unnotified_alerts` | Al arrancar (+30s) | Web Push de alertas perdidas mientras el servicio estaba caído |
| `job_vacuum_db` | Domingos 02:00 | Purga snapshots >1 año y traducciones >30 días; VACUUM SQLite |
| `job_check_claude_health` | Lunes 09:00 | Web Push si la API de Claude no responde |

Toda la gestión (tickers, posiciones, alertas, Trade Republic) se realiza desde el dashboard web.

---

## Dashboard web (web.py)

FastAPI app con autenticación por cookie de sesión. Todas las rutas verifican `_is_auth(session)` y redirigen a `/login` si no está autenticado.

**Rutas disponibles:**

| Ruta | Método | Descripción |
|------|--------|-------------|
| `/` | GET | Dashboard principal: KPIs (valor cartera, alertas activas, último informe, nº tickers), eventos próximos (ex-div/earnings via Alpine.js), cartera con PnL, watchlist por score, modal "añadir posición desde watchlist", gráfico evolución |
| `/oportunidades` | GET | Top activos por horizonte con oportunidad ALTA o MEDIA, columnas adaptadas al plazo |
| `/rebalanceo` | GET | Pesos actuales vs. objetivo + distribución de cartera por horizonte |
| `/screener` | GET | Screener completo con filtros Alpine.js (sector, región, score, drawdown, oportunidad) |
| `/distribucion` | GET | Distribución de cartera por sector, región y **divisa** (USD/EUR/GBP/CHF/JPY) con barras de porcentaje |
| `/simulador` | GET | Simulador de aportación: dado un importe, calcula cuánto comprar de cada posición |
| `/benchmark` | GET | Comparativa cartera vs SPY (S&P500), EWQ (Euro Stoxx) y ticker personalizado (configurable) base 100 |
| `/optimizacion` | GET | Optimización de cartera: Mínima Varianza, Máximo Sharpe (score-aware), Paridad de Riesgo + frontera eficiente |
| `/operaciones` | GET | Historial de operaciones buy/sell por ticker; panel resumen P&L realizado (total invertido, recuperado, comisiones, P&L neto) |
| `/noticias` | GET | Titulares recientes por ticker, traducidos con Claude |
| `/ticker/{ticker}` | GET | Detalle de un ticker: fundamentales, noticias, historial drawdown |
| `/tickers` | GET | Gestión de cartera y watchlist (con precio objetivo, notas/tesis, horizonte) |
| `/posiciones` | GET | Lista de posiciones con PnL calculado |
| `/alertas` | GET | Alertas activas; columna `Stop abs.` para stoploss_pct; score actual del ticker al crear alerta tipo score; campo `expires_at` opcional |
| `/reportes` | GET | Últimos 10 informes Claude |
| `/generar-reporte` | POST | Lanza pipeline completo en thread pool, redirige a `/` |
| `/tickers/add` | POST | Añade ticker a tickers.yaml |
| `/tickers/update` | POST | Actualiza ticker (horizonte, precio objetivo, notas) |
| `/tickers/delete` | POST | Elimina ticker de tickers.yaml |
| `/posiciones/add` | POST | Registra posición (shares + avg_price EUR) |
| `/posiciones/delete` | POST | Elimina posición |
| `/operaciones/add` | POST | Registra operación buy/sell |
| `/operaciones/delete` | POST | Elimina operación por ID |
| `/alertas/add` | POST | Crea alerta; infiere dirección (above/below) del precio actual |
| `/alertas/delete` | POST | Desactiva alerta por ID |
| `/chart/precio/{ticker}` | GET | PNG del precio último año (tema oscuro) |
| `/chart/historial/{ticker}` | GET | PNG del drawdown histórico 30 días (tema oscuro) |
| `/chart/valor-cartera` | GET | PNG de la evolución del valor total de cartera |
| `/chart/benchmark` | GET | PNG comparativa cartera vs SPY/EWQ (base 100) |
| `/chart/frontera-eficiente` | GET | PNG frontera eficiente con Capital Market Line y 4 carteras marcadas |
| `/cartera/valor-historico` | GET | JSON historial valor cartera 90 días |
| `/login` | GET/POST | Formulario de login |
| `/logout` | GET | Borra cookie de sesión |
| `/health` | GET | Estado del sistema: SQLite ok/error, CSV existe, timestamp UTC |

**Filtros Jinja2 registrados:**
- `eur` — formatea como `€1,234.56`; devuelve `—` para None/NaN
- `pct` — formatea como `+1.5%`
- `num` — formatea con 2 decimales
- `dd_class` — CSS class según severidad del drawdown (`text-negative`, `text-warning`, etc.)
- `pnl_class` — CSS class positivo/negativo
- `opp_class` — badge CSS según clasificación ALTA/MEDIA/BAJA
- `tg` — convierte formato Markdown Telegram (`*bold*`, `_italic_`, `` `code` ``) a HTML

**Autenticación:**
- Credenciales en `data/credentials.json` (bcrypt), TOTP en `data/totp_secret.key` (chmod 600)
- Primer acceso fuerza cambio de usuario/contraseña y configuración de 2FA TOTP
- Sesiones por UUID en dict `_active_sessions` (expiran en 30 días, se invalidan al hacer logout)
- Bloqueo de IP tras 5 intentos fallidos (15 min)
- CSRF token global (`CSRF_TOKEN`) inyectado en todos los templates via `templates.env.globals`; validado en todos los POST mutantes
- Cookie `session` con `httponly=True`, `samesite=strict`, expira en 30 días
- `/generar-reporte` tiene rate limit adicional de 2/minuto

**Nuevas rutas:**

| Ruta | Método | Descripción |
|------|--------|-------------|
| `/sw.js` | GET | Service Worker para Web Push (sin autenticación) |
| `/manifest.json` | GET | Manifest PWA (sin autenticación) |
| `/icon-192.png` / `/icon-512.png` | GET | Iconos PWA generados con matplotlib |
| `/push/vapid-public-key` | GET | Clave pública VAPID para suscripción del navegador |
| `/push/subscribe` | POST | Registra suscripción push del navegador |
| `/push/unsubscribe` | POST | Elimina suscripción push (body JSON con `endpoint` + `csrf_token`) |
| `/push/test` | POST | Envía notificación push de prueba a todas las suscripciones activas |
| `/export/portfolio` | GET | Descarga cartera como CSV |
| `/export/watchlist` | GET | Descarga watchlist como CSV |
| `/tickers/import` | POST | Importa tickers desde CSV |
| `/reportes?page=N` | GET | Paginación de informes (10 por página) |
| `/login/totp` | GET/POST | Verificación 2FA TOTP |
| `/setup/first-login` | GET/POST | Wizard de primer acceso |
| `/2fa/setup` | GET/POST | Gestión 2FA para usuario autenticado |
| `/2fa/disable` | POST | Desactiva 2FA |
| `/settings/credentials` | GET/POST | Cambiar usuario/contraseña |
| `/settings/benchmark-ticker` | POST | Guarda ticker personalizado para comparación en benchmark |
| `/api/upcoming-events` | GET | JSON con ex-dividends y earnings próximos (≤7 días) del portfolio |

**Tooltips de ayuda en tablas:**
- Icono ⓘ clickable en cada cabecera de tabla (cartera, watchlist, posiciones)
- Al pulsar muestra un bocadillo con la descripción del concepto; se cierra al hacer clic fuera
- Implementado con un `<div id="help-tooltip">` global en `base.html` y `position: fixed` para no ser recortado por el `overflow-x: auto` de `.table-wrap`
- Función `showHelp(el, text)` en JS global; las descripciones no repiten el término del encabezado

**Generación de gráficos:**
- `_make_price_chart` — precio de cierre 252 días, marca máximo 52 semanas y precio actual, área sombreada en rojo entre precio actual y máximo
- `_make_history_chart` — drawdown desde máximo 52 semanas (últimos 30 días del radar), área roja bajo cero
- Caché HTTP de 5 min (`Cache-Control: public, max-age=300`)
- Las operaciones yfinance se ejecutan en `_executor` (ThreadPoolExecutor, 4 workers) para no bloquear el event loop

## Notas de implementación

- Los gráficos usan tema oscuro (background `#161b22`), backend Agg (sin display)
- La traducción de titulares usa Claude con caché en memoria + BD (TTL 24h); fallback a inglés
- Los mensajes Telegram largos se dividen automáticamente para evitar errores de Markdown
- El healthcheck Docker: bot verifica SQLite cada 60s; web hace GET a `/login` cada 30s
- No hay suite de tests automatizados; se valida manualmente via Telegram o `scheduler.py`
- `generate_csv.py` continúa si falla un ticker individual (recopila errores al final)
- El dashboard web usa un executor de hilos para no bloquear al generar informes
- El timeout de Claude (120s) está configurado en el constructor `anthropic.Anthropic(timeout=120)`, no en `messages.create()`
- CSV en memoria cacheado 5 min (`_csv_cache`); se invalida al generar reporte
- Optimización de cartera cacheada 5 min (`_opt_cache`); se invalida junto con `_csv_cache` al generar reporte
- Si el job diario falla, se notifica automáticamente por Telegram con el error

## Trampas conocidas y decisiones de diseño

- **SQLite como única fuente de verdad**: la arquitectura migró de CSV + tickers.yaml a SQLite puro. `generate_csv.py` ya no escribe fichero CSV; `_read_csv()` en web.py lee de `get_latest_snapshot_as_df()`. `tickers.yaml` solo se importa una vez (migración automática en `init_db()` si la tabla `tickers` está vacía). Ambos ficheros se pueden borrar sin que la aplicación falle.
- **Alpine.js `:style` + `style` estático**: si `:style` devuelve `''`, Alpine sobreescribe el atributo `style` completo y elimina `width`/`height`. Siempre consolidar todo en un único `:style` concatenando la parte estática + dinámica.
- **Jinja2 y métodos dict**: `entry.items`, `cat_items.items()`, etc. resuelven al método Python `dict.items()` en lugar de la clave. Usar `entry['items']` o `dictsort` para iterar dicts en templates.
- **NaN en Python es truthy**: `if cap_eur` pasa cuando `cap_eur` es NaN. Usar siempre `is not None` + `math.isnan()` o el helper `_is_nan()` de web.py.
- **`avg_price` siempre en EUR**: el campo `portfolio.avg_price` debe introducirse en EUR independientemente de la bolsa del ticker. El sistema convierte el precio actual a EUR para calcular P&L.
- **Sufijos de bolsa en yfinance**: tickers europeos requieren sufijo (`.SW`, `.PA`, `.MC`, `.DE`, `.ST`, `.L`, `.AS`, `.MI`). Sin sufijo, yfinance devuelve el ticker americano homónimo si existe, o falla. Causa posiciones duplicadas si se registran con y sin sufijo.
- **MPLCONFIGDIR en Docker**: sin esta variable, matplotlib intenta crear `~/.config/matplotlib` dentro del contenedor como `appuser`, lo que falla si el disco está lleno o hay problemas de permisos. Configurado a `/app/output/.matplotlib` en docker-compose.yml.
- **Templates se reconstruyen con la imagen**: los templates están embebidos en la imagen Docker. Cambios en `templates/` requieren `docker compose pull && docker compose up -d` para que surtan efecto en producción.

## Mantenimiento de la base de datos

La BD SQLite está en `data/radar.db` (volumen montado). Para operaciones directas desde el host:

```bash
# Sin sqlite3 instalado en el contenedor, usar Python:
docker exec market-radar-ai python3 -c "import sqlite3; conn = sqlite3.connect('/app/data/radar.db'); [print(r) for r in conn.execute('SELECT ticker, shares, avg_price FROM portfolio')]; conn.close()"

# Eliminar posiciones duplicadas o incorrectas:
docker exec market-radar-ai python3 -c "import sqlite3; conn = sqlite3.connect('/app/data/radar.db'); conn.execute(\"DELETE FROM portfolio WHERE ticker IN ('TICKER1', 'TICKER2')\"); conn.commit(); conn.close()"
```

Nota: el contenedor `market-radar-ai` se llama así en docker-compose. El contenedor web es `market-radar-web`.

## Jobs periódicos (scheduler.py)

| Job | Cuándo | Descripción |
|-----|--------|-------------|
| `job_daily_report` | Diario a `REPORT_HOUR:00` | Reporte completo con IA + Web Push |
| `job_check_exdividend` | Diario a 07:00 | Web Push si ex-dividend en ≤3 días |
| `job_check_earnings` | Diario a 07:00 | Web Push si earnings en ≤7 días |
| `job_check_price_alerts` | Cada hora | Comprueba alertas precio/drawdown/score/stoploss + Web Push |
| `job_check_sector_concentration` | Cada 24h | Web Push si sector supera umbral |
| `job_replay_unnotified_alerts` | Al arrancar (+30s) | Web Push de alertas perdidas |
| `job_vacuum_db` | Domingos 02:00 | Purga datos >1 año y VACUUM SQLite |
| `job_check_claude_health` | Lunes 09:00 | Web Push si Claude API no responde |

## Campos extendidos en tickers.yaml

Cada ticker admite los siguientes campos en su metadata:
- `name` — nombre del activo
- `block` — sector (ej. "Tecnología", "Financiero")
- `region` — región (ej. "USA", "Europa", "Asia-Pacífico")
- `target_weight` — peso objetivo en cartera (%) para rebalanceo
- `horizon` — horizonte de inversión: `largo`, `medio` o `corto`
- `target_price` — precio objetivo en EUR para calcular upside/downside
- `notes` — tesis de inversión o notas libres (máx. 500 chars)

## Correcciones técnicas implementadas

- **Matplotlib thread-safety**: `_chart_lock = threading.Lock()` en bot.py y web.py; usado en todas las funciones que generan gráficos.
- **Race condition CSV cache**: `_csv_cache_lock = threading.RLock()` en web.py protege lecturas/invalidaciones concurrentes.
- **Memory leak sesiones**: `_cleanup_expired_state()` elimina sesiones y tokens expirados; se llama desde `_is_auth()` con throttle de 60s.
- **VACUUM SQLite**: `_vacuum_lock = threading.Lock()` en database.py evita bloqueos concurrentes.
- **Inyección YAML**: `_sanitize_name()` en web.py limpia caracteres de control del campo nombre antes de guardar en tickers.yaml.
- **Contraseña inicial**: se escribe en `data/initial-password.txt` (chmod 600) en lugar de logs; el archivo se elimina tras cambiar credenciales.
- **Validación alertas**: alertas de drawdown validan rango [-100, 0]; alertas de score validan rango [0, 100].
- **TR timeout**: `asyncio.wait_for(..., timeout=30)` en comandos `/tr_setup` y `/sync_tr`.
- **TR fallback**: ImportError capturado si el módulo `trade_republic` no está instalado.
- **Schema tickers.yaml**: `_validate_tickers_schema()` comprueba estructura básica tras carga.
- **Índice BD**: `idx_price_history_ticker_date` en `(ticker, date DESC)` para acelerar `get_ticker_history`.
- **CSRF rotation thread-safe**: `_rotate_csrf_if_needed()` usa double-checked locking con `_csrf_lock` para evitar race condition entre workers.
- **`_fig_to_response` try/finally**: `plt.close(fig)` se llama en `finally` para garantizar liberación de memoria incluso si `savefig` falla.
- **Dead code alertas**: bloque inalcanzable `if condition_type in ("drawdown", "score")` eliminado de `alertas_add` (los casos ya retornan antes).
- **`stoploss_pct` en historial de alertas**: `alertas.html` no mostraba el badge ni la condición correcta para alertas de tipo `stoploss_pct` en la sección de historial (solo en alertas activas). Añadido `{% elif h_ctype == 'stoploss_pct' %}` en ambos bloques.
- **`tickers_search` max length**: consultas de más de 50 caracteres retornan `[]` sin llamar a yfinance.
- **scheduler.py usa `effective()`**: `send_telegram` lee `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` desde BD (con fallback a env) en tiempo de ejecución.
- **`get_portfolio_value_history` SQL corregido**: usar filtro por fecha (`WHERE date >= date('now', ?)`) en lugar de `LIMIT` que devolvía los registros más antiguos.
- **Config sin sys.exit**: `config.py` no hace `sys.exit()` al arrancar; todas las vars son opcionales con defaults vacíos.
- **Bot wait loop**: `bot.py main()` espera en bucle hasta que `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` estén disponibles (BD o env) antes de arrancar.
- **`ai_analysis.py` y `fetch_data.py` leen config de BD**: `_get_client()` y `_effective_model()` leen `ANTHROPIC_API_KEY` y `MODEL` desde BD en cada invocación.
- **`iterrows()` eliminado de web.py y scheduler.py**: todos los bucles sobre DataFrames usan `.to_dict("records")` para evitar la sobrecarga de crear una `pd.Series` por fila.
- **Caché TTL de traducciones**: `_translate_cache` en `fetch_data.py` usa helpers `_translate_cache_get/set` con TTL de 24h para evitar memory leak en sesiones largas.
- **Push paralelo**: `send_push_to_all()` en `push_utils.py` usa `ThreadPoolExecutor` (máx. 8 workers) para enviar notificaciones en paralelo.
- **Push tri-state**: `send_push_notification()` devuelve `True` (éxito), `False` (410 Gone, suscripción expirada → eliminar) o `None` (error temporal de red/5xx → conservar suscripción). `send_push_to_all()` solo elimina la suscripción cuando el resultado es `False`, no en errores temporales.
- **`_currency_cache` en scheduler.py**: `fast_info.get("currency")` se cachea por ticker para evitar llamadas extra a yfinance en el job de alertas.
- **Rate limit en `/tickers/search`**: `@limiter.limit("10/minute")` añadido para prevenir abuso del endpoint de búsqueda.
- **Paralelismo en generate_csv.py**: `_process_ticker()` se ejecuta en `ThreadPoolExecutor` (hasta 10 workers, configurable con env `FETCH_WORKERS`). Las posiciones se pre-fetchen en una sola query en lugar de N llamadas individuales.
- **`save_snapshot()` usa `executemany()`**: inserta todos los snapshots en una sola operación en vez de N `execute()` separados.
- **Jobs scheduler paralelizados**: `job_check_price_alerts()`, `job_check_exdividend()` y `job_check_earnings()` usan `ThreadPoolExecutor` para fetches de yfinance en paralelo.
- **`get_macro_context()` paralelo**: SPY, VIX y TNX se descargan en paralelo con 3 workers.
- **`_fx_cache` con TTL de 1h**: el caché de tipos de cambio expira a los 60 min en lugar de vaciarse completamente con `clear_fx_cache()`, lo que evita re-fetches innecesarios entre jobs.
- **`_get_positions()` con caché de 60 s**: sustituye `get_all_positions()` en ~22 endpoints de web.py; se invalida al modificar/añadir/eliminar posiciones.
- **`_get_scored_df(df)` con caché de score**: `score_by_horizon()` solo se recalcula cuando cambia el CSV (TTL compartido); el resultado se reutiliza en todos los endpoints del mismo ciclo.
- **`_get_ticker_hist(ticker)` con caché de 1 h**: los históricos yfinance para gráficos se cachean en memoria; `/chart/precio/{ticker}` no descarga de nuevo hasta que expire.
- **`/tickers/enrich` paralelo**: los fetches de yfinance para enriquecer tickers se ejecutan con hasta 5 workers concurrentes (antes secuencial, 10 tickers ≈ 30 s → ≈ 6 s).
- **`get_latest_snapshot_as_df()` usa `ROW_NUMBER() OVER`**: reemplaza el `GROUP BY + INNER JOIN` por window function, más eficiente en tablas grandes.
- **`_invalidate_csv_cache()` limpia todos los cachés dependientes**: al generar nuevo reporte también invalida `_scored_cache` e `_hist_cache` para garantizar coherencia.
- **`_get_ticker_hist()` en todos los endpoints**: correlación, riesgo, optimización y benchmark usan el caché de 1 h; segunda carga es instantánea.
- **Backtesting pre-fetch paralelo**: los históricos de 2 años de todos los tickers únicos se descargan en paralelo antes de iterar los rows del scoring histórico.
- **Prompt caching Claude**: `_call_claude(use_cache=True)` activa `cache_control: ephemeral` en el informe diario; ahorra tokens en reintentos y llamadas repetidas.
- **Double-checked locking en `_get_ticker_hist()`**: evita thundering herd cuando dos requests piden el mismo ticker con caché expirado; la clave incluye `period` para evitar colisiones entre `1y` y `2y`.
- **Bulk prefetch en `generate_csv.py`**: `get_all_trends()` y `get_all_recent_tickers()` reemplazan las N conexiones SQLite paralelas (una por ticker); 2 queries totales antes del ThreadPoolExecutor.
- **`generate_csv.py` sin `clear_fx_cache()`**: eliminada llamada redundante (el caché ya tiene TTL de 1h).
- **Índices en `settings(key)` y `tr_cache(key)`**: aceleran `get_setting()` y operaciones Trade Republic.
- **`clear_fx_cache()` eliminada**: función dead code retirada de `fetch_data.py`; el caché FX tiene TTL de 1h y se renueva automáticamente.
- **KPIs en dashboard**: 4 tarjetas en `/` (valor total cartera, alertas activas, fecha último informe, nº tickers); se calculan en el servidor sin requests adicionales.
- **Widget eventos próximos**: `/api/upcoming-events` devuelve JSON con ex-dividends y earnings ≤7 días; el dashboard los carga con Alpine.js `x-init` en background.
- **Modal "añadir posición desde watchlist"**: botón "+" en cada fila de watchlist del dashboard abre modal Alpine.js que envía al endpoint `/posiciones/add` existente.
- **P&L realizado en operaciones**: panel resumen en `/operaciones` con total invertido, recuperado, comisiones y P&L neto calculados en el servidor.
- **Exposición por divisa**: `/distribucion` añade tercer panel con distribución por divisa (USD/EUR/GBP/CHF/JPY) derivada del campo `region` del snapshot.
- **Stop abs. en alertas**: columna calculada en alertas activas de tipo `stoploss_pct`; muestra `avg_price × (1 - pct/100)`.
- **Alertas con expiración**: campo `expires_at TEXT` en `price_alerts`; `get_active_alerts()` filtra las expiradas; formulario con input date opcional.
- **Benchmark configurable**: form en `/benchmark` para guardar ticker personalizado en `settings("benchmark_ticker")`; `/chart/benchmark` incluye 3ª línea si está configurado.
- **Alerta automática desde target_price**: checkbox en `/tickers/update`; si marcado y `target_price` definido, crea `price_alert` automáticamente con dirección inferida del precio actual.
- **Score actual en formulario de alertas**: `/alertas` pasa dict `{ticker: score}` al template; Alpine.js muestra el score actual al seleccionar tipo `score`.
- **`tojson` Jinja2 filter**: registrado en `templates.env.filters` para serializar Python dicts/lists a JSON seguro en templates.
- **`fiscalidad_page` crash sin operaciones**: `_compute()` retornaba `[]` cuando no había operaciones registradas; la línea de desempaque `fifo_ops, annual_summary, unrealized = await ...` lanzaba `ValueError`. Corregido a `return [], {}, []`.
- **Dead code en `montecarlo_page`**: `pct_curves` y `paths_sample_3y` se calculaban pero nunca se usaban ni pasaban al template. Eliminados; el gráfico se genera independientemente en `/chart/montecarlo`.
- **Template huérfano `export_pdf.html`**: fichero eliminado del disco; la ruta `/export/pdf` ya no existe en el backend.
- **XSS modal watchlist (dashboard.html)**: `onclick` inline con `{{ row.name }}` podía romper el HTML si el nombre contenía comillas dobles. Reemplazado con `data-ticker` y `data-name` attributes leídos via `this.dataset.*`.
- **Import muerto `clear_fx_cache` (scheduler.py)**: función eliminada de `fetch_data.py` pero seguía importándose y llamándose en `job_check_price_alerts()`. Eliminados import y llamada.
- **P&L realizado sin comisiones (web.py)**: `pnl_realized = total_sold - total_bought` no descontaba comisiones. Corregido: `pnl_net = total_sold - total_bought - total_commissions`.
- **Stat card divisas sin contexto (distribucion.html)**: la tarjeta "Divisas distintas" solo mostraba el número. Añadido `stat-sub` con los códigos de divisa (ej. EUR · USD · GBP).
- **`/api/upcoming-events` lento**: scheduler ahora guarda resultado en `settings("upcoming_exdiv_cache")` y `settings("upcoming_earnings_cache")` tras cada job; el endpoint lee de BD en lugar de llamar a yfinance en tiempo real.

## Módulo de Recomendaciones (`discovery.py`)

Descubrimiento automático de oportunidades fuera de la cartera/watchlist.

**Pipeline:** universo → fetch yfinance paralelo → scoring → top 6/horizonte → Claude → BD → `/recomendaciones`

**Universo curado (~300 tickers):**
- S&P 100 (USA, sin sufijo)
- DAX 40 (`.DE`), CAC 40 (`.PA`), FTSE 100 (`.L`), IBEX 35 (`.MC`), Euro Stoxx 50
- Asia-Pacífico: ADRs + HK directos
- Mineras de metales preciosos: GOLD, NEM, WPM, AEM, FNV, RGLD, PAAS, AG, HL, KGC, etc.
- Fuente: Wikipedia vía `pd.read_html()` + base hardcoded (fallback si Wikipedia falla)
- Caché en `settings("discovery_universe")` con TTL de 30 días

**Scoring:** mismos pesos que `scoring.py` (`_compute_score`, `_WEIGHTS`). Horizonte inferido automáticamente con `suggest_horizon()`.

**Claude:** 1 llamada con los 18 candidatos (6×3 horizontes) → 2 frases por ticker (razonamiento + riesgo).

**Caché resultados:** tabla `market_discoveries`. TTL 24h. Columnas: ticker, name, sector, region, horizon, score, opportunity, price_eur, métricas técnicas/fundamentales, claude_analysis, rank_in_horizon, generated_at.

**Rutas web:**
- `GET /recomendaciones` — página con cards por horizonte (largo/medio/corto)
- `POST /recomendaciones/refresh` — lanza regeneración en background (ThreadPoolExecutor)
- `POST /recomendaciones/add-to-watchlist` — añade ticker a watchlist con horizon/sector/region

**Scheduler:** job `job_discovery` lunes 08:30 (semanal, solo si datos >24h).

**Notas:**
- `_discovery_lock` (threading.Lock) evita ejecuciones simultáneas
- Tickers ya en cartera/watchlist se excluyen del análisis
- `refresh_universe()` fuerza regeneración borrando caché de BD

## Trabajo pendiente / próximas funcionalidades

- **Tests automatizados**: no hay suite de tests. Validación manual via scheduler/web.
- **CSRF no en formularios de login/setup**: los endpoints de login no necesitan CSRF (no requieren sesión previa); el CSRF token global cubre todos los formularios de usuario autenticado.
- **Web Push require HTTPS en producción**: los Service Workers solo se registran en orígenes seguros. En `localhost` funciona sin TLS. En producción se necesita reverse proxy con TLS (Cloudflare Tunnel, Caddy, nginx).
## CI/CD

GitHub Actions en `.github/workflows/docker-publish.yml`:
- Trigger: push a `main` o `workflow_dispatch`
- Build multi-arquitectura con QEMU (amd64 + arm64)
- Publica en `ghcr.io/dazanestor/market-radar-ai:latest` + tag SHA
