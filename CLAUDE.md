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
SECURITY.md         # Documento formal SGSI: política, riesgos, activos, criptografía, incidentes, DPIA
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
- **Memory leak sesiones y lockout dicts**: `_cleanup_expired_state()` elimina sesiones, tokens expirados y entradas caducadas de `_failed_logins` / `_account_failed`; se llama desde `_is_auth()` con throttle de 60s (ISO 27001 A.12).
- **`touch_session_db()` en `_is_auth()`**: actualiza `last_seen` en BD para sesiones en memoria, permitiendo auditoría de actividad (ISO 27001 A.12.4).
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
- **Cabeceras de seguridad HTTP (OWASP A05)**: middleware `_refresh_csrf_global` añade `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `X-XSS-Protection: 1; mode=block`, `Referrer-Policy: strict-origin-when-cross-origin` y `Permissions-Policy` en todas las respuestas.
- **CSRF bug en rutas Recomendaciones**: `/recomendaciones/refresh` y `/recomendaciones/add-to-watchlist` llamaban `_validate_csrf(request, form)` (2 args, 1 esperado → TypeError en producción). Corregido a `_require_csrf(request, form.get("csrf_token"))`.
- **Rate limit en operaciones costosas (OWASP A04)**: `@limiter.limit("2/minute")` añadido a `/tickers/enrich` (yfinance paralelo para todos los tickers) y `/recomendaciones/refresh` (yfinance + Claude para ~300 tickers).
- **Content-Security-Policy (OWASP A05)**: cabecera CSP añadida en el middleware — `default-src 'self'`, `script-src 'self' 'unsafe-inline'` (requerido por Alpine.js), `style-src 'self' 'unsafe-inline'`, `img-src 'self' data:`, `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`.
- **Alpine.js self-hosted (OWASP A06 / SRI)**: eliminados CDN de Tailwind (sin uso) y CDN de Alpine.js de `base.html`. Alpine.js 3.14.9 se descarga durante el build Docker (`curl` + `unpkg.com`) y se sirve desde `/static/alpine.min.js`. FastAPI monta `static/` con `StaticFiles`.
- **Divulgación de info en `/health` (OWASP A05)**: el endpoint ya no expone el mensaje de excepción SQLite en la respuesta; solo devuelve `"db_error"` genérico.
- **Validación de path params de ticker (OWASP A03)**: `/chart/precio/{ticker}`, `/chart/historial/{ticker}` y `/ticker/{ticker}` ahora validan contra `_TICKER_RE` y lanzan HTTP 400 si el valor no es válido, evitando path traversal / inyección.
- **Cookie `secure=` configurable (OWASP A07)**: variable `COOKIE_SECURE` (env `COOKIE_SECURE=1`) activa el flag `Secure` en la cookie de sesión para despliegues HTTPS. En desarrollo HTTP permanece desactivado.
- **Validación de endpoint push con `urlparse` (OWASP A10)**: `/push/subscribe` usa `urlparse` para exigir `scheme == 'https'` y `netloc` no vacío en lugar de un simple `startswith`, evitando bypass con URLs malformadas.
- **`treemap.html` sin `onerror` inline (OWASP A03)**: eliminado manejador `onerror` inline con `innerHTML`. Reemplazado por `addEventListener('error', ...)` en bloque `<script>` separado; usa `textContent` en lugar de `innerHTML`.
- **Todos los templates sin `onerror` inline (OWASP A03)**: eliminados 9 manejadores `onerror` inline restantes en `correlacion.html`, `dashboard.html` (×2), `montecarlo.html`, `optimizacion.html`, `riesgo.html` (×2), `ticker_detalle.html` (×2), `tr_historial.html`. Reemplazados por `addEventListener` en bloques `<script>` separados.
- **Inyección de parámetros en redirects (OWASP A03)**: `?saved={t}` y `?error={saved}` en `posiciones_page`, `posiciones_add` y `operaciones_add` usaban f-strings sin codificar. Reemplazado por `quote(val, safe='')` de `urllib.parse`.
- **Validación `_TICKER_RE` en `posiciones_add` (OWASP A03)**: `t = ticker.strip().upper()` sin validación de formato antes del redirect. Añadido `_TICKER_RE.match(t)` igual que en otros endpoints.
- **Rate limit en `/tickers/info` (OWASP A04)**: endpoint que llama a yfinance sin límite. Añadido `@limiter.limit("30/minute")`.
- **Validación de contenido en `/tickers/search` (OWASP A03/A10)**: solo se validaba longitud del parámetro `q`. Añadido `_SEARCH_RE = re.compile(r'^[A-Za-z0-9 .\\-]{2,50}$')` para rechazar URLs y caracteres especiales antes de pasarlos a yfinance.
- **SHA-256 de Alpine.js en Dockerfile (OWASP A08 / ISO 27001 A.12.6)**: añadido `sha256sum -c -` tras la descarga de `alpine.min.js` para detectar compromiso de unpkg.com o MITM durante el build. Hash fijado: `3ed1eed252488921df65e363d6715deb04d7f92aaedb9e52199fdf73cb1e0ad3`.
- **P&L realizado sin comisiones (web.py)**: `pnl_realized = total_sold - total_bought` no descontaba comisiones. Corregido: `pnl_net = total_sold - total_bought - total_commissions`.
- **Stat card divisas sin contexto (distribucion.html)**: la tarjeta "Divisas distintas" solo mostraba el número. Añadido `stat-sub` con los códigos de divisa (ej. EUR · USD · GBP).
- **`/api/upcoming-events` lento**: scheduler ahora guarda resultado en `settings("upcoming_exdiv_cache")` y `settings("upcoming_earnings_cache")` tras cada job; el endpoint lee de BD en lugar de llamar a yfinance en tiempo real.
- **Rate limits endurecidos (ISO 27001 A.12.2)**: `/login`, `/login/totp`, `/setup/first-login` reducidos de 5/min a 3/min; añadidos `@limiter.limit` a `/2fa/setup` (5/min), `/2fa/disable` (3/min), `/gdpr/export` (2/min), `/api/upcoming-events` (60/min), `/cartera/valor-historico` (30/min), charts precio/historial/valor-cartera/benchmark (20/min), charts correlación/riesgo/frontera/montecarlo (10/min).
- **Account-level lockout (ISO 27001 A.9.2.5)**: `_account_failed` dict keyed por username; `_ACCOUNT_LOCKOUT_MAX = 10`, `_ACCOUNT_LOCKOUT_DURATION = 1800s`; complementa el bloqueo por IP para defender contra ataques desde múltiples IPs o proxies.
- **Username hash en audit_log**: TODOS los eventos de audit_log usan `uname_hash` (SHA-256 truncado 16 chars) en lugar del nombre de usuario en texto plano: `login_failed`, `login_locked`, `login_success`, `password_expired`, `credentials_changed`. Evita exposición de credenciales en logs.
- **Invalidación total de sesiones al cambiar credenciales (ISO 27001 A.9.2.6)**: `/settings/credentials` POST llama a `delete_all_sessions_db()` + `_active_sessions.clear()` antes de crear nueva sesión — invalida TODAS las sesiones activas del usuario (no solo la corriente) para prevenir acceso con credenciales antiguas desde otras sesiones concurrentes.
- **Límite de sesiones concurrentes (ISO 27001 A.9.2.3)**: `_MAX_CONCURRENT_SESSIONS = 5`; `_create_session()` rota la sesión más antigua si se supera el límite; implementado con `count_active_sessions_db()` y `get_oldest_session_id_db()`.
- **Limpieza automática de push subscriptions (ISO 27701 Art. 5)**: `purge_old_push_subscriptions(days=90)` en `database.py`; llamada desde `job_vacuum_db()` cada domingo.
- **CSP en Service Worker (OWASP A05)**: `/sw.js` añade cabecera `Content-Security-Policy: default-src 'none'; script-src 'self'; connect-src 'self'`.
- **CSRF `None` check en `/push/unsubscribe`**: corregido `body.get("csrf_token", "")` a `body.get("csrf_token")` para evitar bypass con string vacío que pasaría la validación.
- **Verificación de contraseña actual al cambiar credenciales (ISO 27001 A.9.2.1)**: `/settings/credentials` POST requiere campo `current_password`; si no coincide con el hash almacenado, rechaza el cambio y loguea `credentials_change_rejected`; el template `settings_credentials.html` muestra el campo con nota de seguridad.
- **Permisos 0o600 en credentials.json validados al cargar**: `_load_credentials()` comprueba `os.stat()` y corrige permisos si son más permisivos; el fichero ya se creaba con 0o600 al escribir.
- **Timeout de 10s en SQLite (ISO 27001 A.12 — disponibilidad)**: `sqlite3.connect(DATABASE, timeout=10.0)` en `_db()`; `timeout=30.0` en `vacuum_db()`. Previene bloqueos indefinidos por contención WAL entre el scheduler y el dashboard.
- **Constantes de longitud máxima (_MAX_NAME_LEN, _MAX_NOTES_LEN, _MAX_BLOCK_LEN, _MAX_REGION_LEN)**: aplicadas en `/tickers/add`, `/tickers/update` y `/operaciones/add`; sustituyen hardcoded `[:500]` por constantes named (ISO 27001 A.14.2).
- **/health sin información interna (ISO 27001 A.5)**: eliminados `snapshot_date` y `timestamp` del endpoint público; solo devuelve `{"status": "ok"}` o `{"status": "db_error"}` con HTTP 503.
- **Dockerfile: imagen Python pineada a 3.12.10-slim (ISO 27001 A.12.6)**: builds reproducibles; la versión exacta se actualiza cada ciclo de mantenimiento.
- **TOTP secret con validación de integridad (ISO 27001 A.10.1.1)**: `_totp_secret()` valida formato base32 (`_TOTP_B32_RE`) al cargar el fichero; si el contenido es inválido o corrompido, logea `totp_integrity_error` en audit_log y trata 2FA como deshabilitado en lugar de fallar silenciosamente.
- **VAPID_CONTACT_EMAIL configurable (ISO 27001 A.16.1.2)**: `push_utils.py` lee `VAPID_CONTACT_EMAIL` del entorno en lugar de usar `"admin@localhost"` hardcodeado; emite warning en startup si no está configurado; añadido a `.env.example` con documentación.
- **Warning dashboard: API key en BD (ISO 27001 A.10)**: si `ANTHROPIC_API_KEY` está en la tabla `settings` (texto plano), se muestra aviso en el dashboard recordando asegurar `data/radar.db`.
- **Warning dashboard: VAPID email (ISO 27001 A.16)**: si `VAPID_SUBJECT` sigue siendo `localhost`, se muestra aviso en el dashboard.
- **Mensajes de lockout diferenciados**: `/login` pasa `error=ip_locked` (15 min) o `error=account_locked` (30 min) según el tipo de bloqueo; `login.html` muestra mensajes específicos para cada caso.
- **Data Processing Register (Art. 30 RGPD)**: añadido en `SECURITY.md` sección 10.3 con tabla de todas las actividades de tratamiento: cartera, operaciones, alertas, push subscriptions, sesiones, audit log, Anthropic.
- **Procedimiento de notificación de brecha (Art. 33/34 RGPD)**: añadido en `SECURITY.md` sección 7.2 con plazo 72h AEPD, criterios Art. 34, template de notificación y registro de incidentes (sección 7.4).
- **Log de pruebas de restauración de backup**: añadida tabla en `SECURITY.md` sección 8.3 con script de test y registro de resultados trimestrales.
- **DPA Anthropic documentada**: `SECURITY.md` sección 10.2 actualizada con URL del DPA, mecanismo SCCs Module One, sub-procesadores (AWS) y contacto de privacidad.
- **Username hash en TODOS los eventos audit_log (ISO 27001 A.12.4 / ISO 27701)**: extendido a `login_success`, `password_expired` y `credentials_changed` — todos los eventos usan `uname_hash` (SHA-256 hex 16 chars); ningún evento registra el nombre de usuario en texto plano.
- **Invalidación total de sesiones al cambiar credenciales (ISO 27001 A.9.2.6)**: `delete_all_sessions_db()` + `_active_sessions.clear()` antes de crear la nueva sesión; previene que sesiones concurrentes antiguas sigan válidas tras cambio de contraseña.
- **Validación de fecha futura en operaciones (ISO 27001 A.14.2)**: `/operaciones/add` rechaza fechas de operación futuras con `error=fecha_futura`; solo acepta `date <= datetime.date.today()`.
- **Audit log de cambios en cartera (ISO 27001 A.12.4)**: `/posiciones/add`, `/posiciones/delete` y `/operaciones/add` registran eventos `position_upserted`, `position_deleted` y `operation_added` con ticker, shares y precio en audit_log.
- **TR_PHONE tipo password (ISO 27701 Art. 5 — minimización)**: campo `TR_PHONE` en `_APP_SETTINGS` cambia de `type="text"` a `type="password"` para evitar exposición del número de teléfono en pantalla.
- **Docker logging con rotación (ISO 27001 A.12.4.3)**: servicios `market-radar` y `market-radar-web` usan `driver: json-file` con `max-size: 10m` y `max-file: 5`; previene pérdida de logs por overflow y crecimiento ilimitado en disco.
- **Handler global de excepciones 500 (OWASP A05 / ISO 27001 A.5)**: `_generic_exception_handler` registrado en FastAPI; captura cualquier excepción no manejada, logea en audit_log como `unhandled_exception` y devuelve `{"detail": "Error interno del servidor"}` sin stack trace ni información interna.
- **Audit log completo para todos los endpoints POST mutantes (ISO 27001 A.12.4)**: añadidos eventos `ticker_added`, `ticker_updated`, `ticker_deleted`, `alert_created`, `alert_deleted`, `operation_deleted`, `report_triggered`, `push_subscribed`, `push_unsubscribed` — ahora todos los cambios de datos generan traza de auditoría.
- **Cache-Control: no-store en /gdpr/export (ISO 27701 Art. 20)**: la respuesta de exportación de datos personales incluye `Cache-Control: no-store, no-cache, must-revalidate, max-age=0` para evitar caching en navegador o proxies intermedios.
- **Límite de tamaño en /tickers/import (ISO 27001 A.12.2)**: upload de CSV rechazado si supera 2 MB para prevenir DoS por archivos grandes.
- **Docker security_opt: no-new-privileges (ISO 27001 A.5.7 — Menor privilegio)**: ambos servicios añaden `security_opt: [no-new-privileges:true]` para evitar escalada de privilegios desde el proceso del contenedor.
- **SBOM en CI/CD (ISO 27001 A.12.6)**: `anchore/sbom-action` genera SBOM en formato SPDX-JSON tras el build; artefacto `sbom-spdx` retenido 365 días para trazabilidad de supply chain.
- **Cache-Control: no-store en /export/portfolio y /export/watchlist (ISO 27001 A.5)**: los CSV de exportación incluyen `no-store, no-cache` igual que `/gdpr/export`; datos financieros no se cachean en el navegador.
- **Audit log completo en endpoints faltantes (ISO 27001 A.12.4)**: `tickers_imported` (con conteo importado/errores), `push_test_sent`; `/recomendaciones/add-to-watchlist` registra `ticker_added` con `origen=recomendaciones`.
- **Validación de entrada en /settings/app POST (ISO 27001 A.14.2)**: `_valid_setting()` valida REPORT_HOUR (0-23), TIMEZONE (ZoneInfo), ANTHROPIC_API_KEY (≤500 chars), límite genérico 2000 chars; valores inválidos se ignoran en lugar de guardarse.
- **Truncado de campos en /tickers/import (ISO 27001 A.14.2)**: `nombre`, `bloque` y `region` en CSV importado usan constantes `_MAX_NAME_LEN`, `_MAX_BLOCK_LEN`, `_MAX_REGION_LEN` igual que el resto de endpoints.
- **Servicio backup hardening (ISO 27001 A.12.3 / A.5.7)**: `security_opt: no-new-privileges:true` + `mem_limit: 256m` + `cpus: 0.25` para prevenir escalada de privilegios y consumo excesivo de recursos.
- **requirements.txt versiones exactas (ISO 27001 A.12.6)**: `cryptography==44.0.3`, `scipy==1.15.3`, `pytest==8.3.5` — todas las dependencias usan `==` para builds reproducibles y deterministas.
- **Clave VAPID privada en fichero (ISO 27001 A.10.1.2)**: `push_utils.py` ya no guarda la clave privada ECDH en la tabla `settings` (texto plano en BD). Se almacena en `data/vapid_private.pem` con `chmod 0o600`. Migración automática al arrancar si existe clave antigua en BD; se elimina de BD tras migrar. La clave pública (no sensible) permanece en BD.
- **Validación de contenido SVG en QR (ISO 27001 A.14.2)**: `_make_qr_svg()` valida con `_SVG_DANGEROUS_RE` que el SVG generado no contiene `<script>`, `javascript:` ni handlers `on*=` antes de marcarlo como `Markup` seguro.
- **Límite de tamaño en informes Claude (ISO 27001 A.12.2)**: `save_report()` trunca contenido si supera 200 KB (`_MAX_REPORT_LEN = 200_000`) para prevenir crecimiento ilimitado de BD.
- **`job_check_security_events` ampliado (ISO 27001 A.12.4)**: añadidos `login_locked` y `unhandled_exception` a los eventos críticos monitorizados; antes solo incluía `gdpr_delete`, `totp_disabled` y `credentials_changed`.
- **`_cleanup_expired_state()` purga lockout dicts (ISO 27001 A.12)**: además de sesiones y pending_tokens, limpia entradas expiradas de `_failed_logins` y `_account_failed`; previene memory leak en dicts de bloqueo de IP y cuenta en sesiones largas.
- **`touch_session_db()` en `_is_auth()` (ISO 27001 A.12.4)**: actualiza `last_seen` en BD para sesiones activas en memoria; permite auditoría de actividad de sesión y detección de sesiones inactivas.
- **`push_subscriptions` en `/gdpr/export` (ISO 27701 Art. 20 — portabilidad)**: la exportación de datos personales incluye suscripciones push con `endpoint_hash` (SHA-256 16 chars) y `user_agent`; el endpoint completo no se exporta para minimizar exposición de datos de terceros.
- **`entrypoint.sh` con `set -e` y permisos VAPID (ISO 27001 A.10.1.2)**: el script de entrypoint Docker falla rápido (`set -e`) y corrige permisos de `vapid_private.pem` a 0o600 al arrancar si el fichero existe.
- **Retry con backoff exponencial en `_fetch_price` (ISO 27001 A.12 — disponibilidad)**: `tenacity` aplica 3 intentos con espera exponencial (2–10s) antes de marcar el precio como no disponible; reduce falsos negativos en alertas por fallos transitorios de yfinance.
- **Docker `cap_drop: ALL` en todos los servicios (ISO 27001 A.5.7 — mínimo privilegio)**: `market-radar`, `market-radar-web` y `backup` eliminan todas las capabilities Linux; previene escalada de privilegios incluso si el proceso es comprometido.
- **Docker `mem_limit` y `cpus` en servicios principales (ISO 27001 A.5.7)**: `market-radar` limitado a 768MB/1 CPU; `market-radar-web` a 512MB/0.5 CPU; previene DoS por agotamiento de recursos del host.
- **Rate limit en `/gdpr/delete` (1/minute) y `/settings/app` POST (5/minute) (ISO 27001 A.12.2)**: endpoints mutantes sin rate limit quedan cubiertos; previene abuso de operaciones costosas o destructivas.
- **TOTP brute-force counter (ISO 27001 A.9.2.2)**: `_totp_failed_attempts` dict cuenta fallos por pending token; tras `_TOTP_MAX_ATTEMPTS = 3` intentos fallidos se revoca el token y se fuerza re-login completo; `_cleanup_expired_state()` purga contadores de tokens expirados.
- **`pytr` pineado a commit específico en requirements.txt (ISO 27001 A.12.6)**: dependencia de GitHub pineada a SHA `afdfeef` para builds reproducibles y seguros; evita supply chain compromise si se sube código malicioso a la rama main de pytr.
- **Dockerfile `chmod 555` en entrypoint.sh (ISO 27001 A.5.7)**: entrypoint es ejecutable pero no modificable por ningún usuario tras el build; previene modificaciones del script de arranque en tiempo de ejecución.
- **`pool.map(..., timeout=120)` en jobs paralelos del scheduler (ISO 27001 A.12 — disponibilidad)**: `job_check_price_alerts`, `job_check_exdividend` y `job_check_earnings` ahora tienen timeout de 120s en el `pool.map`; si yfinance se bloquea en un ticker, el job completa con datos parciales en lugar de colgar indefinidamente.
- **TruffleHog secret scanning en CI/CD (ISO 27001 A.12.6)**: step `trufflesecurity/trufflehog@main` detecta secretos verificados en commits antes de Bandit SAST; artefactos de todos los scans retenidos ≥90 días.
- **Sanitización de errores en push notifications (ISO 27001 A.5 — minimización de info)**: scheduler.py ya no incluye `type(e).__name__` ni `str(e)` en mensajes Web Push; todos los errores del reporte diario usan mensajes genéricos para evitar fuga de detalles técnicos al servicio de push externo.
- **`purge_old_market_discoveries(days=7)` (ISO 27701 Art. 5 — minimización)**: función añadida en `database.py`; llamada desde `job_vacuum_db()` cada domingo; elimina descubrimientos de mercado con más de 7 días, alineando el ciclo de vida con el TTL declarado.
- **Rate limit en `/gdpr` GET (10/min) y `/audit-log` GET (10/min) (ISO 27001 A.12.4 / A.12.2)**: evita enumeración del log de auditoría y abuso del formulario GDPR.
- **Audit log en rotación de sesión concurrente (ISO 27001 A.9.2.3)**: `_create_session()` registra evento `session_rotated_max_concurrent` cuando invalida la sesión más antigua por superar `_MAX_CONCURRENT_SESSIONS`.
- **Audit log en restauración de sesiones (ISO 27001 A.12.4)**: `_load_sessions_from_db()` registra evento `sessions_restored_from_db` con contador al arrancar; permite detectar si sesiones fueron manipuladas en BD entre reinicios.
- **Validación completa de `expires_at` en alertas (ISO 27001 A.14.2)**: rechaza fechas pasadas (`error=fecha_pasada`), fechas >10 años en el futuro (`error=fecha_lejana`) y formatos inválidos (`error=fecha_invalida`); antes solo se ignoraban silenciosamente.
- **Rate limits en todos los POST mutantes (ISO 27001 A.12.2 / OWASP A04)**: añadido `@limiter.limit` a 11 endpoints que carecían de él: `/tickers/add` (20/min), `/tickers/update` (20/min), `/tickers/delete` (10/min), `/posiciones/add` (20/min), `/posiciones/delete` (10/min), `/operaciones/add` (20/min), `/operaciones/delete` (10/min), `/alertas/add` (20/min), `/alertas/delete` (10/min), `/alertas/reactivar` (10/min), `/push/subscribe` (10/min).
- **Validación de longitud en `/push/subscribe` (ISO 27001 A.14.2)**: `endpoint`, `p256dh` y `auth` limitados a 500 chars cada uno; HTTP 400 si se supera el límite.
- **`_safe_for_prompt()` en `ai_analysis.py` (ISO 27001 A.14.2 — prompt injection)**: función `_safe_for_prompt()` elimina caracteres de control (U+0000–U+001F, U+007F) de campos de usuario (`notes`, tesis) antes de incluirlos en prompts Claude; previene inyección de instrucciones en el LLM.
- **`_safe()` en `discovery.py` (ISO 27001 A.14.2 — prompt injection)**: función local `_safe()` sanitiza nombres y sectores procedentes de yfinance antes de incluirlos en prompts Claude; datos de terceros (yfinance) pueden contener caracteres de control si la fuente es comprometida.
- **Timeout 30s en `get_macro_context()` (ISO 27001 A.12 — disponibilidad)**: `as_completed(..., timeout=30)` evita bloqueo indefinido si SPY/VIX/TNX no responden; el job_daily_report completa con datos parciales en lugar de colgarse.
- **Rate limit 5/minute en endpoints Claude on-demand (ISO 27001 A.12.2)**: `@limiter.limit("5/minute")` añadido a `/ticker/{ticker}/analizar`, `/rebalanceo/sugerencia` y `/noticias/analizar`; los tres invocan Claude API y carecían de protección anti-DoS/quota-drain.
- **`PRAGMA foreign_keys=ON` en `_db()` (ISO 27001 A.12.1 — integridad)**: activada la comprobación de integridad referencial en SQLite; antes las restricciones FK estaban definidas en el schema pero SQLite las ignoraba silenciosamente.
- **Mensaje push de integrity_check sanitizado (ISO 27001 A.5)**: `job_vacuum_with_integrity()` ya no incluye el resultado literal del `PRAGMA integrity_check` en el cuerpo del push; usa mensaje genérico e imprime el detalle solo en logs internos.
- **Timeout Claude en `discovery.py` reducido a 30s (ISO 27001 A.12 — disponibilidad)**: `anthropic.Anthropic(timeout=30)` en lugar de 90s; alineado con el timeout del resto del sistema; evita bloquear el job semanal de descubrimientos si Claude tarda.
- **Rate limits en 9 POST endpoints sin cobertura (ISO 27001 A.12.2 / OWASP A04)**: añadido `@limiter.limit` a `/settings/credentials` (3/min), `/settings/benchmark-ticker` (10/min), `/tr/setup/start` (5/min), `/tr/setup/complete` (5/min), `/tr/sync` (3/min), `/tickers/move-to-portfolio` (10/min), `/tickers/import` (2/min), `/push/unsubscribe` (10/min), `/recomendaciones/add-to-watchlist` (10/min); ninguno tenía protección anti-abuso.
- **Validación de formato TOTP antes de verificar (ISO 27001 A.9.4)**: `_verify_totp()` comprueba que el código es solo dígitos y no supera 10 caracteres antes de pasarlo a pyotp; previene timing attacks y bypass con entradas anómalas.
- **HEALTHCHECK en Dockerfile (ISO 27001 A.12.1 — disponibilidad)**: instrucción `HEALTHCHECK` añadida; verifica conexión SQLite cada 60s con timeout de 10s y start-period de 30s; el orquestador detecta el proceso caído automáticamente sin necesidad de polling externo.
- **HSTS con directiva `preload` (ISO 27001 A.10 / OWASP A05)**: cabecera `Strict-Transport-Security` incluye ahora `; preload` cuando `COOKIE_SECURE=1`; permite registrar el dominio en la lista HSTS preload de navegadores para proteger contra ataques de downgrade en primera visita.
- **`X-Permitted-Cross-Domain-Policies: none` (OWASP A05)**: cabecera añadida en el middleware de seguridad; bloquea las políticas cross-domain de Flash/Silverlight y previene exfiltración de datos por clientes legacy.
- **Charts con `Cache-Control: private` (ISO 27001 A.5 — minimización de exposición)**: los endpoints de gráficos PNG cambian de `public` a `private`; los datos financieros de la cartera no se almacenan en cachés compartidas (proxies, CDN) donde terceros podrían acceder.
- **`--forwarded-allow-ips` restringido a `127.0.0.1` (ISO 27001 A.9.2 — anti-spoofing)**: uvicorn ya no acepta cualquier IP en `X-Forwarded-For`; solo confía en `127.0.0.1` como fuente de la IP real del cliente; previene elusión del rate limiter e IP-based lockout via header spoofing.
- **Audit log en `/settings/app` POST (ISO 27001 A.12.4)**: cada cambio de configuración genera evento `setting_changed` o `setting_deleted` en audit_log; valores de `API_KEY`/`PASSWORD`/`SECRET` se enmascaran como `***`; antes no había traza de cambios de configuración.
- **`_check_db_integrity()` al arrancar (ISO 27001 A.12.1 — integridad)**: `init_db()` ejecuta `PRAGMA integrity_check(1)` antes de crear tablas; si la BD está corrupta, el proceso aborta con error claro en lugar de continuar silenciosamente sobre datos inválidos.
- **Jinja2 auto-escaping explícito (ISO 27001 A.14.2 — XSS prevention)**: `Jinja2Templates` inicializado con `autoescape=select_autoescape(["html", "xml"])`; no depende de los defaults del framework, garantizando protección XSS estable ante futuros upgrades de Jinja2/FastAPI.
- **Content-Type validation en `/push/unsubscribe` (ISO 27001 A.14.2 — input validation)**: verifica `Content-Type: application/json` antes de llamar a `request.json()`; devuelve HTTP 415 si no coincide; previene ataques de content-type confusion.
- **Restricción instancia única documentada en SECURITY.md (ISO 27001 A.12.2)**: el rate limiter `slowapi` usa almacenamiento en memoria; añadida advertencia explícita en SECURITY.md indicando que la aplicación no debe escalarse horizontalmente (multi-réplica anularía rate limiting e IP lockout).
- **`audit_log` incluido en `/gdpr/export` (ISO 27701 Art. 9 / RGPD Art. 20)**: los eventos de auditoría (logins, cambios de credenciales, etc.) contienen datos personales (IP, timestamp, tipo de evento) y ahora se exportan en la descarga de portabilidad; antes solo se exportaban cartera, operaciones, alertas, tickers y suscripciones push.
- **`SECURITY.md` excluido de la imagen Docker (ISO 27001 A.5)**: añadido a `.dockerignore`; el documento contiene análisis de riesgos, matrices de amenazas y procedimientos de respuesta a incidentes que no deben distribuirse en la imagen de producción.
- **TTL de suscripciones Web Push en política de privacidad (ISO 27701 Art. 5 / RGPD Art. 13)**: añadida fila «Suscripciones Web Push: 90 días» a la tabla de plazos de conservación en `privacy.html`; el plazo ya se aplicaba via `purge_old_push_subscriptions()` pero no estaba declarado al usuario.
- **Validación de formato TOTP en `/setup/first-login` (ISO 27001 A.9.4)**: el handler de primer login llamaba a `pyotp.TOTP.verify()` directamente sin validar que el código sea solo dígitos y ≤10 chars; ahora usa la misma validación que `_verify_totp()`.
- **Digest de imagen publicada en CI/CD (ISO 27001 A.12.6)**: paso `docker inspect --format RepoDigests` tras el build registra el digest SHA256 en el log de CI; proporciona traza de auditoría de supply chain para cada imagen publicada en GHCR.
- **Red Docker personalizada `radar-net` (ISO 27001 A.13.1 — segmentación de red)**: `market-radar` y `market-radar-web` se conectan a una red bridge privada `radar-net`; el servicio `backup` usa `network_mode: none` (sin acceso a red — solo al volumen de datos); aislamiento respecto a contenedores de otros stacks en el mismo host.
- **Complejidad de contraseña — carácter especial obligatorio (ISO 27001 A.9.4.3)**: `_validate_password()` exige ahora al menos un carácter no alfanumérico además de letra y dígito; contraseñas existentes no se ven afectadas hasta el próximo cambio.
- **Procedimiento de rotación de `BACKUP_PASSPHRASE` (ISO 27001 A.10.1.2)**: documentado en `SECURITY.md` sección 4.2: longitud mínima ≥16 chars, rotación anual o tras incidente, instrucciones para preservar la clave antigua hasta que expiren los backups afectados (7 días).
- **Detalle de excepción TR sanitizado en HTTPException (ISO 27001 A.5 / OWASP A05)**: `/chart/tr/*` ya no incluye `str(e)` en el `detail` de HTTPException 503; el error se registra en log interno y el cliente recibe mensaje genérico.
- **NOT NULL en columnas críticas de schema (ISO 27001 A.12.1 — integridad)**: `price_history.ticker`, `price_history.date`, `price_alerts.ticker`, `price_alerts.direction`, `price_alerts.condition_type`, `alert_history.ticker`, `alert_history.triggered_at` declaradas NOT NULL en CREATE TABLE; previene inserción de NULLs que romperían constraints UNIQUE y lógica de alertas.
- **Mensajes de error sanitizados en `generate_csv.py` (ISO 27001 A.5)**: `str(e)` eliminado de mensajes de error que se propagan al scheduler y logs de usuario; la excepción completa se registra internamente con `logger.exception()`.
- **`/robots.txt` (ISO 27001 A.5 — minimización de información)**: endpoint sin autenticación que devuelve `Disallow: /`; previene indexación del dashboard privado por motores de búsqueda.
- **Shutdown handler para `_executor` (ISO 27001 A.12 — disponibilidad)**: `@app.on_event("shutdown")` llama a `_executor.shutdown(wait=True)`; garantiza cierre ordenado de tareas pendientes (gráficos, yfinance) antes de terminar el proceso, evitando posible corrupción de datos.

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

## Tests automatizados

Suite de tests en `tests/` ejecutable con `python -m pytest tests/ -v`.

**138 tests, 0.6 s.** Sin dependencias externas (yfinance, anthropic, etc.): se mockean con `MagicMock` en `conftest.py`. La BD se redirige a un SQLite temporal por test.

| Fichero | Módulo | Cobertura |
|---------|--------|-----------|
| `test_scoring.py` | `scoring.py` | `_has_data`, `_compute_score`, `_opportunity_label`, `suggest_horizon`, `get_weights`, `score_watchlist`, `score_by_horizon` |
| `test_database.py` | `database.py` | Settings, posiciones, tickers, alertas, historial de alertas, informes, operaciones, valor de cartera, caché de noticias, suscripciones push, discoveries, price_history |
| `test_push_utils.py` | `push_utils.py` | `_b64url_encode/decode`, `_make_vapid_jwt`, comportamiento `send_push_to_all` (mocked) |
| `test_discovery.py` | `discovery.py` | `_calc_rsi`, `_infer_region`, `_score_and_classify` |

Para ejecutar:
```bash
python -m pytest tests/ -v
```

## Trabajo pendiente / próximas funcionalidades

- **Tests automatizados**: ✅ implementado — 138 tests en `tests/`.
- **OWASP hardening para producción**: ✅ implementado — CSP, Alpine.js self-hosted, validación path params, `Cookie secure=`, push urlparse, info disclosure `/health`, treemap onerror, rate limits, security headers.
- **ISO 27001 + ISO 27701**: ✅ implementado — ver sección "Cumplimiento ISO 27001/27701" más abajo.
- **CSRF no en formularios de login/setup**: los endpoints de login no necesitan CSRF (no requieren sesión previa); el CSRF token global cubre todos los formularios de usuario autenticado.
- **Web Push require HTTPS en producción**: los Service Workers solo se registran en orígenes seguros. En `localhost` funciona sin TLS. En producción se necesita reverse proxy con TLS (Cloudflare Tunnel, Caddy, nginx).
- **Activar `COOKIE_SECURE=1` en producción**: añadir al `.env` cuando el dashboard esté detrás de un reverse proxy HTTPS.

## Cumplimiento ISO 27001 / ISO 27701

### Controles implementados

#### ISO 27001 A.9 — Control de acceso
- **Autenticación 2FA**: TOTP (pyotp, RFC 6238) + bcrypt cost 12; TOTP secret validado como base32 al cargar
- **Sesiones persistentes en BD** (`sessions` table): sobreviven reinicios; TTL 30 días; carga al arrancar desde `get_all_active_sessions_db()`
- **Expiración de contraseña**: 90 días (`_PASSWORD_EXPIRY_DAYS = 90`); se comprueba en cada login; aviso a 15 días; fuerza cambio al expirar
- **Bloqueo de IP**: 5 intentos fallidos → 15 minutos (`_LOCKOUT_MAX`, `_LOCKOUT_DURATION`)
- **Bloqueo de cuenta**: 10 intentos fallidos (cualquier IP) → 30 minutos (`_ACCOUNT_LOCKOUT_MAX`, `_ACCOUNT_LOCKOUT_DURATION`); defiende frente a ataques distribuidos con rotación de IPs
- **Límite de sesiones concurrentes**: máximo 5 sesiones activas simultáneas; la más antigua se invalida al crear la nueva (`_MAX_CONCURRENT_SESSIONS = 5`)
- **Regeneración de sesión**: nueva sesión creada tras cambio de credenciales para prevenir session fixation
- **Mensajes de lockout diferenciados**: login muestra mensaje específico para bloqueo por IP (15 min) vs. bloqueo de cuenta (30 min)

#### ISO 27001 A.10 — Criptografía
- **Web Push**: AES-GCM + ECDH RFC 8291 con `cryptography>=42.0.0` (declarado explícito en requirements.txt)
- **Backups cifrados**: AES-256-CBC con PBKDF2 via `openssl enc`; activar con `BACKUP_PASSPHRASE` en `.env`; restaurar con `openssl enc -d -aes-256-cbc -pbkdf2`

#### ISO 27001 A.12.4 — Registro y auditoría
- **Tabla `audit_log`**: registra `login_success`, `login_failed`, `login_locked`, `logout`, `totp_success`, `totp_failed`, `totp_enabled`, `totp_disabled`, `credentials_changed`, `password_expired`, `gdpr_export`, `gdpr_delete`
- **Visor paginado**: `GET /audit-log` — tabla con tipo de evento, IP, timestamp, detalles
- **Purga automática**: `purge_old_audit_log(days=365)` ejecutado cada domingo en `job_vacuum_db()`

#### ISO 27001 A.12.6 / A.14.2 — Escaneo de vulnerabilidades en CI/CD
- **Bandit SAST**: análisis estático Python en cada push a `main`; artefacto `bandit-security-report` retenido 90 días
- **pip-audit**: escaneo de CVEs en dependencias Python; artefacto `pip-audit-report` retenido 90 días
- **Trivy**: escaneo de imagen Docker post-publicación (HIGH + CRITICAL); SARIF subido a GitHub Security tab

#### ISO 27001 A.14 — Seguridad en desarrollo
- **CSP**: `Content-Security-Policy` en todas las respuestas (middleware `_refresh_csrf_global`)
- **Cabeceras HTTP**: X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
- **HSTS**: `Strict-Transport-Security: max-age=31536000; includeSubDomains` cuando `COOKIE_SECURE=1`
- **COOP**: `Cross-Origin-Opener-Policy: same-origin` en todas las respuestas
- **SQL parameterizado**: todas las queries usan placeholders `?`

#### ISO 27701 Art. 7 — Consentimiento y privacidad
- **Política de privacidad**: `GET /privacy` — página con finalidad, plazos, medidas de seguridad, derechos
- **Sección GDPR**: `GET /gdpr` — hub de derechos del interesado

#### ISO 27701 Art. 9 — Derechos del interesado
- **Portabilidad (Art. 20 RGPD)**: `GET /gdpr/export` — descarga JSON con cartera, operaciones, alertas, tickers; loggea evento `gdpr_export`
- **Derecho al olvido (Art. 17 RGPD)**: `POST /gdpr/delete` — elimina portfolio, operations, price_alerts, alert_history, push_subscriptions, sessions, portfolio_value; requiere confirmación "BORRAR"; loggea evento `gdpr_delete`

### Nuevas tablas en BD

- **`audit_log`**: `id`, `event_type`, `ip_address`, `details`, `created_at` — índice en `created_at DESC` y `event_type`
- **`sessions`**: `session_id PK`, `ip_address`, `user_agent`, `created_at`, `expires_at`, `last_seen` — índice en `expires_at`

### Nuevas rutas web

| Ruta | Método | Descripción |
|------|--------|-------------|
| `/privacy` | GET | Política de privacidad ISO 27701 |
| `/gdpr` | GET | Hub de derechos GDPR: exportar, borrar, auditoría |
| `/gdpr/export` | GET | Descarga JSON portabilidad de datos |
| `/gdpr/delete` | POST | Elimina datos personales (confirmación requerida) |
| `/audit-log` | GET | Visor paginado de eventos de auditoría |

### Nuevas funciones en `database.py`

- `log_audit_event(event_type, ip_address, details)` — inserta evento en audit_log
- `get_audit_log(limit, offset, event_type)` — consulta eventos con paginación
- `count_audit_log()` — total de eventos
- `purge_old_audit_log(days=365)` — purga eventos antiguos
- `create_session_db(session_id, expires_at, ip_address, user_agent)` — persiste sesión
- `get_session_db(session_id)` — busca sesión no expirada
- `touch_session_db(session_id)` — actualiza last_seen
- `delete_session_db(session_id)` — elimina sesión
- `delete_expired_sessions_db()` — purga sesiones expiradas
- `get_all_active_sessions_db()` — sesiones activas para restaurar al arrancar
- `delete_all_sessions_db()` — purga total (GDPR)
- `gdpr_delete_personal_data()` — elimina datos personales (GDPR Art. 17)

### Nuevos jobs en scheduler.py (segunda ronda)

| Job | Cuándo | Qué hace |
|-----|--------|----------|
| `job_check_security_events` | Cada hora | Web Push si ≥5 `login_failed` en última hora o eventos críticos |
| `job_vacuum_with_integrity` | Domingos 01:50 | `PRAGMA integrity_check` antes del VACUUM; Web Push si falla |
| `job_cleanup_sessions` | Diario 03:00 | `delete_expired_sessions_db()` para mantener tabla `sessions` limpia |

### Avisos de seguridad en dashboard

- `security_warnings` calculado en el servidor y pasado al template de `/`
- Muestra banner rojo/amarillo si: `COOKIE_SECURE` no está activo, contraseña próxima a expirar (≤15 días), o contraseña ya expirada

### Integridad de backups

- Cada backup (cifrado o en claro) genera también un fichero `.sha256` con `sha256sum`
- Los `.sha256` rotan junto con los backups (se conservan los últimos 7)

### Variables de entorno nuevas

- `BACKUP_PASSPHRASE` — frase de cifrado para backups AES-256-CBC (recomendado en producción)
- `COOKIE_SECURE` — activa HSTS + cookies Secure (requerido detrás de HTTPS reverse proxy)

### Documento formal SGSI

- **`SECURITY.md`**: política SGSI, matriz de riesgos (12 riesgos), inventario de activos (9 activos), tabla de algoritmos criptográficos, procedimiento de respuesta a incidentes, DPIA Anthropic, RTO ≤4h / RPO ≤24h, checklist auditoría anual.

## CI/CD

GitHub Actions en `.github/workflows/docker-publish.yml`:
- **`security-scan`** (job nuevo, se ejecuta antes de build): Bandit SAST + pip-audit; artefactos retenidos 90 días
- **`build-and-push`** depende de `security-scan` (no publica si el escaneo no termina)
- Trigger: push a `main` o `workflow_dispatch`
- Build multi-arquitectura con QEMU (amd64 + arm64)
- Publica en `ghcr.io/dazanestor/market-radar-ai:latest` + tag SHA
- **Trivy** post-publicación: escaneo imagen Docker (HIGH+CRITICAL), tabla en log + SARIF a GitHub Security
