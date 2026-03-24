# Market Radar AI

Herramienta de monitoreo de cartera e inversiones. Descarga datos de mercado, calcula métricas técnicas y fundamentales, puntúa oportunidades y genera análisis diarios con Claude (Anthropic). Las notificaciones llegan al navegador via Web Push (PWA).

---

## Tabla de contenidos

- [Características](#características)
- [Arquitectura](#arquitectura)
- [Requisitos](#requisitos)
- [Obtener la API key de Anthropic](#obtener-la-api-key-de-anthropic)
- [Instalación y despliegue](#instalación-y-despliegue)
- [Despliegue con Portainer](#despliegue-con-portainer-stacks)
- [Configuración](#configuración)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Base de datos](#base-de-datos)
- [Scoring](#scoring)
- [Flujo de datos](#flujo-de-datos)
- [Seguridad](#seguridad)
- [Tests](#tests)
- [Ejecución standalone](#ejecución-standalone)

---

## Características

- Monitoreo de cartera personal y watchlist de acciones
- Descarga de precios históricos, dividendos y fundamentales via yfinance
- **Todos los precios convertidos automáticamente a EUR** con tipos de cambio en tiempo real
- Scoring multi-factor con **pesos diferenciados por horizonte de inversión** (corto/medio/largo plazo), incluyendo RSI(14)
- Análisis diario automatizado con Claude (Anthropic) incluyendo contexto macro
- Noticias recientes por ticker en el análisis, con caché en BD (24h TTL)
- Alertas de precio, drawdown, score y stop-loss configurables desde el dashboard web
- Historial de alertas disparadas
- **Notificaciones Web Push (PWA)**: alertas, informe diario y avisos de sistema directamente en el navegador, sin Telegram
- Historial persistente en SQLite con backups automáticos diarios (últimos 7)
- Dashboard web con autenticación segura: usuario/contraseña bcrypt + 2FA TOTP, bloqueo por IP, CSRF
- Exportación de cartera y watchlist a CSV; importación masiva de tickers desde CSV
- Indicador de frescura de datos en el dashboard
- **Historial de operaciones** buy/sell con registro de fecha, precio y notas de inversión
- **Evolución de cartera**: gráfico histórico del valor total, actualizado en cada reporte
- **Distribución por sector y región**: visualización de concentración de la cartera
- **Simulador de aportación**: calcula qué comprar dado un importe para respetar pesos objetivo
- **Comparativa vs benchmark**: rendimiento de cartera vs SPY (S&P500) y EWQ (Euro Stoxx) en base 100
- **Screener reactivo**: filtra todos los tickers por sector, región, score, drawdown y oportunidad
- **Optimización de cartera**: Mínima Varianza, Máximo Sharpe y Paridad de Riesgo con frontera eficiente
- **Precio objetivo y notas por ticker**: `target_price` y `notes` gestionados desde el dashboard
- **Alertas ex-dividend**: aviso automático 3 días antes de la fecha de ex-dividendo
- **Recomendaciones de mercado global**: descubrimiento automático de oportunidades en ~300 acciones (S&P100, DAX, CAC40, FTSE100, IBEX35, EuroStoxx50, mineras de metales preciosos) con análisis cualitativo por Claude, clasificadas por horizonte (largo/medio/corto)
- Despliegue con Docker usando imagen pre-compilada de GitHub Container Registry (GHCR)
- CI/CD con GitHub Actions: build automático multi-arquitectura (amd64 + arm64) en cada push a `main`

---

## Arquitectura

```
database.py (tickers)   ← configuración: cartera + watchlist + metadata por ticker
     |
fetch_data.py           ← descarga datos de yfinance (precios, dividendos, fundamentales, noticias, macro)
     |
generate_csv.py         ← calcula métricas técnicas y fundamentales, guarda snapshots en BD
     |
scoring.py              ← puntúa cada activo con score multi-factor (pesos por horizonte)
     |
ai_analysis.py          ← genera análisis con Claude (Anthropic), adaptado al horizonte por ticker
     |
database.py (snapshots) ← price_history, alertas, reportes, operaciones, valor cartera
     |
push_utils.py           ← Web Push al navegador (informe, alertas, avisos de sistema)
```

> **SQLite es la única fuente de datos.** No se escriben ficheros CSV ni YAML. `tickers.yaml` solo se usa para migración inicial automática la primera vez que arranca (si la tabla `tickers` está vacía); puede borrarse después.

`scheduler.py` ejecuta todos los jobs periódicos como servicio de larga ejecución con APScheduler.

---

## Requisitos

- Docker y Docker Compose
- API key de Anthropic

---

## Obtener la API key de Anthropic

1. Crea una cuenta en [console.anthropic.com](https://console.anthropic.com)
2. Ve a **API Keys** en el menú lateral
3. Pulsa **Create Key**, dale un nombre (ej: `market-radar`) y cópiala

Es tu `ANTHROPIC_API_KEY`. Empieza por `sk-ant-...`

> El modelo por defecto es `claude-haiku-4-5-20251001`, el más económico. Puedes cambiarlo con la variable `MODEL` en el `.env`.

---

## Instalación y despliegue

### 1. Clonar el repositorio

```bash
git clone https://github.com/dazanestor/market-radar-ai.git
cd market-radar-ai
```

### 2. Crear el archivo de variables de entorno

```bash
cp .env.example .env   # o crear manualmente
```

Contenido mínimo del `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...
```

### 3. Arrancar

```bash
docker compose up -d
```

Docker descargará automáticamente la imagen de `ghcr.io/dazanestor/market-radar-ai:latest`. Arrancan cuatro servicios:
- `init` — crea los directorios necesarios con los permisos correctos y termina
- `market-radar` — scheduler con los jobs periódicos (reporte, alertas, etc.)
- `market-radar-web` — dashboard web en el puerto configurado
- `backup` — snapshots diarios cifrados de la base de datos

El reporte diario se ejecuta a las 08:00 (Europe/Madrid por defecto) y llega al navegador via Web Push.

### Parar

```bash
docker compose down
```

---

### Despliegue con Portainer (Stacks)

El servicio `init` crea automáticamente los directorios `data/backups` y `output/.matplotlib` con `chmod 777` para que los contenedores principales puedan escribir en los volúmenes montados.

**1. En Portainer → Stacks → Add stack:**

- Pega el contenido de `docker-compose.yml`
- En **Environment variables** añade al menos: `ANTHROPIC_API_KEY`

**2. Despliega el stack.**

> Gestiona tickers y posiciones desde el dashboard web (`/tickers`, `/posiciones`).

### Ver logs

```bash
docker compose logs -f
```

---

## Configuración

### Variables de entorno

| Variable | Requerida | Default | Descripción |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Sí | — | API key de Anthropic |
| `MODEL` | No | `claude-haiku-4-5-20251001` | Modelo de Claude a usar |
| `REPORT_HOUR` | No | `8` | Hora del reporte diario (formato 24h) |
| `TIMEZONE` | No | `Europe/Madrid` | Zona horaria del reporte (`Europe/Madrid`, `America/New_York`, etc.) |
| `WEB_PORT` | No | `8589` | Puerto del dashboard web |
| `COOKIE_SECURE` | No | — | Establece el flag `Secure` en la cookie de sesión. Poner `1` en producción detrás de HTTPS |
| `MPLCONFIGDIR` | No | `/app/output/.matplotlib` | Directorio de caché de matplotlib (configurado automáticamente en Docker) |
| `TR_PHONE` | No | — | Teléfono Trade Republic (formato `+34600000000`) |
| `TR_PIN` | No | — | PIN de Trade Republic |
| `TR_COOKIES_FILE` | No | `data/tr_cookies.txt` | Ruta al fichero de cookies de Trade Republic |

### Tickers

Los tickers se gestionan íntegramente desde el dashboard web (`/tickers`). La BD almacena por ticker: categoría (portfolio/watchlist), nombre, sector, región, horizonte de inversión, peso objetivo, precio objetivo y notas.

- Los tickers son los símbolos de yfinance. Bolsas europeas requieren sufijo:

| Bolsa | Sufijo | Ejemplo |
|---|---|---|
| Suiza (SIX) | `.SW` | `NESN.SW` |
| París (Euronext) | `.PA` | `OR.PA` |
| Madrid (BME) | `.MC` | `SAN.MC` |
| Frankfurt (XETRA) | `.DE` | `BMW.DE` |
| Milán | `.MI` | `ENI.MI` |
| Amsterdam | `.AS` | `ASML.AS` |
| Estocolmo | `.ST` | `EQT.ST` |
| Londres | `.L` | `SHEL.L` |

---

## Estructura del proyecto

```
market-radar-ai/
├── scheduler.py        # servicio de jobs periódicos (reporte, alertas, vacuum…)
├── web.py              # dashboard web FastAPI (puerto 8589)
├── generate_csv.py     # descarga y calcula métricas por ticker → guarda en BD
├── fetch_data.py       # wrappers de yfinance (datos, noticias, macro)
├── scoring.py          # score multi-factor de oportunidad
├── discovery.py        # recomendaciones de mercado global (Wikipedia + yfinance + Claude)
├── ai_analysis.py      # integración con Claude (Anthropic)
├── push_utils.py       # Web Push VAPID (notificaciones al navegador)
├── database.py         # acceso a SQLite
├── config.py           # variables de entorno
├── Dockerfile
├── docker-compose.yml  # servicios: init, market-radar (scheduler), web, backup
├── requirements.txt
├── templates/          # plantillas Jinja2 del dashboard web
├── static/             # activos estáticos (Alpine.js self-hosted; generado en build Docker)
├── tests/              # suite pytest (138 tests: scoring, database, push_utils, discovery)
│   ├── conftest.py
│   ├── test_scoring.py
│   ├── test_database.py
│   ├── test_push_utils.py
│   └── test_discovery.py
├── .github/
│   └── workflows/
│       └── docker-publish.yml  # CI/CD: build y push a GHCR en cada push a main
├── data/               # base de datos SQLite y credenciales (creado en runtime)
│   ├── radar.db
│   ├── credentials.json
│   ├── totp_secret.key
│   └── backups/        # snapshots automáticos diarios (últimos 7)
└── output/             # caché matplotlib (creado en runtime)
```

---

## Base de datos

SQLite en `data/radar.db`. Tablas:

### `portfolio`
Posiciones registradas manualmente.

| Campo | Tipo | Descripción |
|---|---|---|
| `ticker` | TEXT PK | Símbolo del activo |
| `shares` | REAL | Número de acciones |
| `avg_price` | REAL | Precio medio de compra en EUR |

Poblar con `/posicion` o directamente:
```sql
INSERT INTO portfolio (ticker, shares, avg_price) VALUES ('NESN.SW', 10, 95.50);
```

> **Importante:** `avg_price` debe introducirse siempre en **EUR**, independientemente de la bolsa del ticker. El P&L se calcula comparando este valor con el precio actual convertido a EUR automáticamente por el sistema.

### `price_history`
Snapshot diario de cada ticker. Se guarda una entrada por ticker por día.

| Campo | Descripción |
|---|---|
| `ticker` / `date` | Clave única |
| `price` | Precio de cierre en EUR (convertido automáticamente) |
| `drawdown_52w` | Caída desde máximo 52 semanas (%) |
| `momentum_3m` / `momentum_6m` | Rendimiento 3 y 6 meses (%) |
| `volatility` | Volatilidad anualizada (%) |
| `dividend_yield` | Rentabilidad por dividendo (%) |
| `score` / `opportunity` | Puntuación y nivel de oportunidad |

### `price_alerts`
Alertas activas o disparadas. Soporta cuatro tipos de condición:
- **price** — precio de mercado supera/baja de un umbral
- **drawdown** — drawdown desde máximo 52s supera un umbral
- **score** — score del activo supera un umbral
- **stoploss_pct** — pérdida vs precio de compra supera el % indicado

### `alert_history`
Historial de alertas disparadas: ticker, tipo, valor objetivo, precio en el momento del disparo y timestamp. Campo `notified` para reenviar alertas perdidas durante caídas del bot.

### `reports`
Historial de análisis generados por Claude.

### `operations`
Historial de operaciones buy/sell.

| Campo | Tipo | Descripción |
|---|---|---|
| `ticker` | TEXT | Símbolo del activo |
| `date` | TEXT | Fecha de la operación |
| `type` | TEXT | `buy` o `sell` |
| `shares` | REAL | Número de acciones |
| `price_eur` | REAL | Precio de ejecución en EUR |
| `notes` | TEXT | Notas opcionales |

### `portfolio_value`
Valor total de cartera por día (actualizado en cada reporte diario).

| Campo | Tipo | Descripción |
|---|---|---|
| `date` | TEXT UNIQUE | Fecha del snapshot |
| `total_eur` | REAL | Valor total en EUR |
| `positions_count` | INTEGER | Número de posiciones |

### `news_cache`
Caché de traducciones de titulares (SHA-256 del titular como clave, TTL 24h). Reduce llamadas a la API de Claude.

### `tr_cache`
Caché clave-valor para datos de Trade Republic (`cash_eur`, `tr_transactions`, `tr_unmatched`).

---

## Scoring

Cada activo recibe un score numérico calculado con **7 factores** con pesos que varían según el horizonte de inversión del ticker (`corto`, `medio`, `largo`). El score se calcula tanto para portfolio como para watchlist:

| Factor | Largo plazo | Medio plazo | Corto plazo |
|--------|------------|------------|------------|
| Drawdown 52w | 20% | 25% | 25% |
| Momentum 3m | 5% | 15% | 20% |
| Volatilidad | 15% | 10% | 5% |
| Dividendo | 20% | 10% | 0% |
| ROE | 25% | 15% | 0% |
| PER | 15% | 15% | 0% |
| RSI(14) | 0% | 10% | **50%** |

**Horizontes temporales:**
- `corto`: días – 3 meses. Señales técnicas (RSI, momentum, rebotes).
- `medio`: 3 meses – 18 meses. Mix fundamentales + momentum.
- `largo`: 18 meses – varios años. Calidad del negocio + dividendo.

`suggest_horizon()` infiere el horizonte óptimo automáticamente si no está configurado en `tickers.yaml`.

Clasificación final:
- **ALTA**: score > 15
- **MEDIA**: score > 8
- **BAJA**: score <= 8

---

## Flujo de datos

### Reporte diario (08:00)

```
1. get_macro_context()     → S&P500, VIX, bono 10Y
2. generate()              → descarga precios, obtiene tipos de cambio y convierte a EUR
3. score_watchlist()       → puntúa todos los activos
4. save_snapshot()         → guarda en price_history
5. get_news()              → últimas noticias por ticker
6. analyze()               → envía todo a Claude y obtiene análisis
7. save_report()           → guarda el análisis en BD
8. send_push_to_all()      → Web Push al navegador con resumen del informe
```

### Comprobación de alertas (cada hora)

```
1. get_active_alerts()     → obtiene alertas activas de BD (precio, drawdown, score, stoploss)
2. yf.Ticker.history()     → precio actual por ticker
3. get_latest_snapshot_as_df() → drawdown y score del último snapshot
4. Comprueba condición     → precio below/above, drawdown >, score <, pérdida >
5. Si se cumple:           → Web Push al navegador, desactiva alerta, guarda en alert_history
```

---

## Dashboard web

El `docker-compose.yml` incluye un segundo servicio (`market-radar-web`) que arranca el dashboard web en el puerto `8589`. Se despliega junto al scheduler automáticamente con `docker compose up -d`.

Accede desde el navegador a `http://<IP-servidor>:8589`.

### Funcionalidades

- **Dashboard** — cartera con P&L, watchlist por score, último informe Claude e indicador de frescura de datos; el valor total incluye liquidez de Trade Republic si está disponible; icono ⓘ en cada columna con explicación del concepto al pulsar
- **Rebalanceo** — peso actual vs. objetivo con acción recomendada (Añadir / Recortar / OK)
- **Noticias** — titulares recientes por ticker traducidos al español (con caché 24h); muestra timestamp de actualización
- **Ticker detalle** — fundamentales, métricas técnicas, historial drawdown y noticias
- **Tickers** — añadir/eliminar activos; importar en masa desde CSV; exportar cartera o watchlist a CSV; badge visual para tickers con posición registrada
- **Posiciones** — registrar y eliminar posiciones con P&L en euros; sincronización con Trade Republic; feedback visual al guardar; icono ⓘ en cada columna con explicación del concepto
- **Alertas** — crear alertas de precio, drawdown, score y stop-loss dinámico; historial de alertas disparadas
- **Optimización de cartera** — tres carteras óptimas (Mínima Varianza, Máximo Sharpe, Paridad de Riesgo) con frontera eficiente y Capital Market Line; retornos multi-factor ajustados por horizonte
- **Recomendaciones** — oportunidades de mercado global detectadas automáticamente fuera de la cartera/watchlist; cards por horizonte con métricas y análisis Claude; botón para añadir directamente a watchlist; se actualiza semanalmente o bajo demanda
- **Reportes** — historial paginado de análisis Claude (10 por página)
- **Generar reporte** — lanza el pipeline completo desde el navegador (máx. 2 por minuto); muestra error si el pipeline falla
- **`/health`** — endpoint JSON con estado de la base de datos, existencia del CSV y timestamp UTC; útil para healthchecks externos

### Web Push y PWA

El dashboard es una Progressive Web App (PWA) instalable. Soporta notificaciones push nativas en el navegador sin necesidad de tener la pestaña abierta.

**Funcionamiento:**
1. Al visitar el dashboard, el navegador registra el Service Worker (`/sw.js`)
2. El usuario suscribe su navegador a las notificaciones push desde Ajustes
3. Las alertas de precio/drawdown/score/stop-loss se envían como push al navegador
4. Compatible con Chrome, Firefox, Edge, Safari 16.4+ y como PWA instalada en Android/iOS

**Implementación sin dependencias externas:**
- Claves VAPID generadas con `cryptography` (`data/vapid_private.pem` + `data/vapid_public.pem`)
- Cifrado RFC 8291 (AES-128-GCM + ECDH efímero) implementado en `push_utils.py`
- Suscripciones persistidas en la tabla `push_subscriptions` de SQLite

> **Nota:** Los Service Workers solo funcionan en orígenes seguros (HTTPS). En `localhost` funciona sin TLS. En producción se necesita un reverse proxy con TLS (Cloudflare Tunnel, Caddy, nginx).

### Autenticación

El dashboard usa autenticación segura basada en credenciales almacenadas en `data/credentials.json` (bcrypt):

- **Primer acceso**: en el primer arranque se genera automáticamente una contraseña aleatoria para el usuario `admin`. La contraseña aparece en los logs del contenedor web y se guarda en `data/initial-password.txt` (en el host):
  ```bash
  docker compose logs market-radar-web | grep -A5 "PRIMER ARRANQUE"
  # o bien:
  cat ./data/initial-password.txt
  ```
  Al completar el asistente de primer acceso (cambio de credenciales + configuración de 2FA TOTP), el archivo se elimina automáticamente. Se fuerza un asistente de configuración para establecer usuario y contraseña, seguido de la configuración de 2FA TOTP (compatible con Google Authenticator, Authy, etc.)
- **2FA TOTP**: obligatorio tras el primer acceso; se puede deshabilitar desde `Ajustes → 2FA`
- **Bloqueo por IP**: tras 5 intentos fallidos, la IP queda bloqueada 15 minutos
- **CSRF**: token global en todos los formularios POST
- **Sesiones**: UUID por sesión, expiran en 30 días; se invalidan al hacer logout
- **Cabeceras de seguridad**: CSP, X-Frame-Options, X-Content-Type-Options y más en todas las respuestas (ver sección [Seguridad](#seguridad))

---

## Integración Trade Republic

El sistema puede sincronizar posiciones y saldo de efectivo desde Trade Republic de forma opcional.

### Configuración

1. Añade tus credenciales al `.env`:
   ```env
   TR_PHONE=+34600000000
   TR_PIN=1234
   ```
2. En el dashboard, ve a **Tickers → Trade Republic** y pulsa **Conectar dispositivo**
3. Introduce el código SMS que recibirás en tu móvil para completar la vinculación

### Qué sincroniza

- **Saldo en efectivo** — se suma al valor total de la cartera en el dashboard
- **Posiciones** — intenta mapear los ISINs de Trade Republic a tickers de yfinance usando OpenFIGI; las posiciones no mapeadas se muestran en el dashboard con su nombre e ISIN
- **Historial de transacciones** — buy/sell/dividendos para el historial de operaciones

### Notas

- La integración usa el módulo `trade_republic` (instalación opcional). Si no está instalado, la funcionalidad queda desactivada silenciosamente
- Los datos se cachean en `tr_cache` (SQLite) para no repetir llamadas innecesarias
- La sincronización se hace bajo demanda desde el dashboard (botón **Sincronizar**)
- La sesión de Trade Republic se persiste en `data/tr_cookies.txt`

---

## Seguridad

El dashboard implementa las recomendaciones OWASP Top 10 para despliegues en producción.

### Cabeceras HTTP

Todas las respuestas incluyen:

| Cabecera | Valor |
|---|---|
| `Content-Security-Policy` | `default-src 'self'`; scripts solo desde el propio origen; `object-src 'none'`; `frame-ancestors 'none'` |
| `X-Frame-Options` | `DENY` |
| `X-Content-Type-Options` | `nosniff` |
| `X-XSS-Protection` | `1; mode=block` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` |

### Autenticación y sesiones

- Contraseñas con **bcrypt** (hash + salt)
- **2FA TOTP** obligatorio tras el primer acceso (Google Authenticator, Authy, etc.)
- Cookie de sesión con `HttpOnly`, `SameSite=Strict` y `Secure` (activar con `COOKIE_SECURE=1` en producción HTTPS)
- Bloqueo automático por IP tras 5 intentos fallidos (15 min)
- **CSRF token** global en todos los formularios POST de usuario autenticado
- Sesiones invalidadas al hacer logout

### Dependencias de frontend

Alpine.js se descarga durante el **build Docker** en una versión fijada (`3.14.9`) y se sirve desde `/static/alpine.min.js`. No hay peticiones a CDN externas en tiempo de ejecución, eliminando la dependencia de terceros y el riesgo de compromiso de la cadena de suministro.

### Validación de entradas

- Path params de ticker validados contra regex `[A-Z0-9.^=-]{1,20}` (HTTP 400 si no coincide)
- Endpoint Web Push validado con `urlparse` (requiere `scheme=https` y `netloc` no vacío)
- Alertas de drawdown validadas en rango `[-100, 0]`; alertas de score en `[0, 100]`
- Rate limits en endpoints costosos: `/tickers/enrich`, `/recomendaciones/refresh` (2/min), `/generar-reporte` (2/min), `/tickers/search` (10/min)

### Despliegue en producción

```env
# Añadir al .env en producción (detrás de Cloudflare Tunnel / Caddy / nginx con TLS)
COOKIE_SECURE=1
WEB_PASSWORD=contraseña_segura
```

> Los Service Workers (Web Push) solo se registran en orígenes HTTPS. En `localhost` funciona sin TLS.

---

## Tests

El proyecto incluye una suite de 138 tests pytest que cubren la lógica de negocio principal.

```bash
pip install pytest pandas scipy cryptography
python -m pytest tests/ -v
```

| Módulo | Tests | Qué cubre |
|---|---|---|
| `test_scoring.py` | 39 | `_compute_score`, `_opportunity_label`, `suggest_horizon`, `score_watchlist` |
| `test_database.py` | 52 | CRUD completo en SQLite temporal: tickers, alertas, operaciones, snapshots, push |
| `test_push_utils.py` | 15 | VAPID JWT, b64url, `send_push_to_all` (tri-state: éxito / expirado / error temporal) |
| `test_discovery.py` | 32 | `_calc_rsi`, `_infer_region` (sufijos bolsa), `_score_and_classify` |

Los tests de base de datos usan una BD SQLite temporal aislada via fixture `tmp_db` (monkeypatch). Los módulos externos (yfinance, anthropic, FastAPI) se substituyen con stubs en `conftest.py`.

---

## Ejecución standalone

Si quieres ejecutar un análisis sin el bot:

```bash
python scheduler.py
```

Requiere las mismas variables de entorno. Útil para pruebas o si prefieres gestionar el scheduling externamente (cron, etc.).
