# Market Radar AI

Bot de Telegram para monitoreo de cartera e inversiones. Descarga datos de mercado, calcula métricas técnicas y fundamentales, puntúa oportunidades y genera análisis diarios con Claude (Anthropic).

---

## Tabla de contenidos

- [Características](#características)
- [Arquitectura](#arquitectura)
- [Requisitos](#requisitos)
- [Crear el bot de Telegram](#crear-el-bot-de-telegram)
- [Obtener la API key de Anthropic](#obtener-la-api-key-de-anthropic)
- [Instalación y despliegue](#instalación-y-despliegue)
- [Despliegue con Portainer](#despliegue-con-portainer-stacks)
- [Configuración](#configuración)
- [Comandos del bot](#comandos-del-bot)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Base de datos](#base-de-datos)
- [Scoring](#scoring)
- [Flujo de datos](#flujo-de-datos)
- [Ejecución standalone](#ejecución-standalone)

---

## Características

- Monitoreo de cartera personal y watchlist de acciones
- Descarga de precios históricos, dividendos y fundamentales via yfinance
- **Todos los precios convertidos automáticamente a EUR** con tipos de cambio en tiempo real
- Scoring multi-factor con **pesos diferenciados por horizonte de inversión** (corto/medio/largo plazo), incluyendo RSI(14)
- Análisis diario automatizado con Claude (Anthropic) incluyendo contexto macro
- Noticias recientes por ticker en el análisis, con caché en BD (24h TTL)
- Alertas de precio, drawdown y score configurables desde el dashboard web
- Historial de alertas disparadas
- Gráficos de precio e historial de drawdown enviados por Telegram
- Control completo desde Telegram: añadir/eliminar tickers, registrar posiciones, ver rebalanceo
- `/posicion` añade el ticker al radar automáticamente obteniendo nombre, sector y país de yfinance
- Historial persistente en SQLite con backups automáticos diarios (últimos 7)
- Dashboard web con autenticación segura: usuario/contraseña bcrypt + 2FA TOTP, bloqueo por IP, CSRF
- Exportación de cartera y watchlist a CSV; importación masiva de tickers desde CSV
- Indicador de frescura de datos en el dashboard
- Notificación automática por Telegram si el reporte diario falla
- **Historial de operaciones** buy/sell con registro de fecha, precio y notas de inversión
- **Evolución de cartera**: gráfico histórico del valor total, actualizado en cada reporte
- **Distribución por sector y región**: visualización de concentración de la cartera
- **Simulador de aportación**: calcula qué comprar dado un importe para respetar pesos objetivo
- **Comparativa vs benchmark**: rendimiento de cartera vs SPY (S&P500) y EWQ (Euro Stoxx) en base 100
- **Screener reactivo**: filtra todos los tickers por sector, región, score, drawdown y oportunidad
- **Optimización de cartera**: Mínima Varianza, Máximo Sharpe y Paridad de Riesgo con frontera eficiente; retornos esperados multi-factor combinando histórico, score del radar, precio objetivo de analistas, momentum y fundamentales
- **Precio objetivo y notas por ticker**: `target_price` y `notes` por ticker, gestionados desde el dashboard
- **Alertas ex-dividend**: aviso automático en Telegram 3 días antes de la fecha de ex-dividendo
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
bot.py                  ← bot de Telegram: jobs periódicos, charts, alertas
     |
database.py (snapshots) ← price_history, alertas, reportes, operaciones, valor cartera
```

> **SQLite es la única fuente de datos.** No se escriben ficheros CSV ni YAML. `tickers.yaml` solo se usa para migración inicial automática la primera vez que arranca (si la tabla `tickers` está vacía); puede borrarse después.

`scheduler.py` es una alternativa standalone (sin bot) para ejecutar el análisis manualmente.

---

## Requisitos

- Docker y Docker Compose
- Token de bot de Telegram (ver instrucciones abajo)
- Chat ID de Telegram (el tuyo, para autorización y notificaciones)
- API key de Anthropic

---

## Crear el bot de Telegram

### 1. Crear el bot con @BotFather

1. Abre Telegram y busca [@BotFather](https://t.me/BotFather)
2. Envía `/newbot`
3. Elige un nombre visible (ej: `Market Radar`)
4. Elige un username único acabado en `bot` (ej: `mi_market_radar_bot`)
5. BotFather te devolverá el **token**: `123456789:ABC-xyz...`

Guárdalo — es tu `TELEGRAM_BOT_TOKEN`.

### 2. Obtener tu Chat ID

1. Busca [@userinfobot](https://t.me/userinfobot) en Telegram
2. Envía cualquier mensaje
3. Te responderá con tu **Id** numérico (ej: `987654321`)

Ese número es tu `TELEGRAM_CHAT_ID`. El bot solo responderá a mensajes de este ID.

### 3. Activar el bot

Busca tu bot por su username en Telegram y envía `/start` para activarlo.

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

Contenido del `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_CHAT_ID=987654321
```

### 3. Arrancar

```bash
docker compose up -d
```

Docker descargará automáticamente la imagen publicada en `ghcr.io/dazanestor/market-radar-ai:latest`. Un init container creará `tickers.yaml` vacío si no existe, y luego arrancará el bot. Los directorios `data/` y `output/` los crea Docker Compose automáticamente al montar los volúmenes. El reporte diario se ejecuta a las 08:00 (Europe/Madrid por defecto).

### Parar

```bash
docker compose down
```

---

### Despliegue con Portainer (Stacks)

El init container crea automáticamente `tickers.yaml` (vacío), `data/` y `output/` con los permisos correctos para que el bot pueda escribir en ellos.

**1. En Portainer → Stacks → Add stack:**

- Pega el contenido de `docker-compose.yml`
- En **Environment variables** añade: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

**2. Despliega el stack.**

> El `tickers.yaml` inicial estará vacío. Usa `/posicion` para añadir posiciones (se añaden al radar automáticamente) o `/agregar` para añadir a la watchlist.

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
| `TELEGRAM_BOT_TOKEN` | Sí | — | Token del bot de Telegram |
| `TELEGRAM_CHAT_ID` | Sí | — | Tu chat ID (único autorizado a usar el bot) |
| `MODEL` | No | `claude-haiku-4-5-20251001` | Modelo de Claude a usar |
| `REPORT_HOUR` | No | `8` | Hora del reporte diario (formato 24h) |
| `TIMEZONE` | No | `Europe/Madrid` | Zona horaria del reporte (`Europe/Madrid`, `America/New_York`, etc.) |
| `WEB_PORT` | No | `8589` | Puerto del dashboard web |
| `MPLCONFIGDIR` | No | `/app/output/.matplotlib` | Directorio de caché de matplotlib (configurado automáticamente en Docker) |
| `TR_PHONE` | No | — | Teléfono Trade Republic (formato `+34600000000`) |
| `TR_PIN` | No | — | PIN de Trade Republic |
| `TR_COOKIES_FILE` | No | `data/tr_cookies.txt` | Ruta al fichero de cookies de Trade Republic |

### Tickers (`tickers.yaml`)

Define los activos monitoreados. Se puede editar manualmente o desde el bot con `/agregar` y `/eliminar`.

```yaml
portfolio:
  V:
    name: Visa
    target_weight: 7      # peso objetivo en cartera (%)
    block: Redes
    region: USA

watchlist:
  COST:
    name: Costco
    block: Consumo
    region: USA
```

- **`portfolio`**: posiciones que ya tienes. Muestra P&L si tienes precio de compra registrado.
- **`watchlist`**: activos que sigues para posibles entradas.
- **`target_weight`**: peso objetivo en % para el análisis de rebalanceo (solo portfolio).
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

## Comandos del bot

El bot solo responde a mensajes del `TELEGRAM_CHAT_ID` configurado.

### Análisis

| Comando | Descripción |
|---|---|
| `/start` | Muestra el menú de ayuda con todos los comandos disponibles |
| `/reporte` | Genera análisis completo con Claude ahora mismo (macro + cartera + watchlist + noticias) |
| `/cartera` | Muestra portfolio con precio, drawdown, momentum, volatilidad y P&L |
| `/watchlist` | Muestra watchlist ordenada por score con indicador de oportunidad (alta/media/baja) |
| `/rebalanceo` | Muestra peso actual vs objetivo de cada posición con acción recomendada |
| `/grafico <ticker>` | Envía chart de precio del último año con máximo de 52 semanas |
| `/fundamentos <ticker>` | Muestra PER, P/B, ROE, margen neto, deuda y noticias recientes de cualquier ticker |
| `/historial <ticker>` | Muestra evolución de drawdown y score de los últimos 30 días + gráfico |
| `/reportes` | Lista los últimos 5 análisis de Claude con fecha |

### Posiciones

| Comando | Descripción |
|---|---|
| `/posicion <ticker> <acciones> <precio>` | Registra posición en cartera (precio en EUR). Si el ticker no está en el radar lo añade automáticamente obteniendo nombre, sector y país de yfinance |
| `/eliminar_posicion <ticker>` | Elimina posición de la cartera |

Ejemplo:
```
/posicion NESN.SW 10 95.50
/posicion V 5 240.00
```

### Alertas de precio

| Comando | Descripción |
|---|---|
| `/alerta <ticker> <precio>` | Crea alerta de precio. Si el precio objetivo es menor al actual → alerta al bajar. Si es mayor → alerta al subir |
| `/mis_alertas` | Lista todas las alertas activas con su ID |
| `/borrar_alerta <id>` | Desactiva una alerta por ID |

Ejemplo:
```
/alerta COST 800      ← avisa cuando Costco baje a 800
/alerta V 280         ← avisa cuando Visa suba a 280
```

Las alertas se comprueban cada hora automáticamente. Una vez disparada, se desactiva y queda registrada en el historial.

Desde el dashboard web también puedes crear alertas por **drawdown** (ej: avisa cuando el drawdown supere el 20%) y por **score** (ej: avisa cuando el score baje de 10).

### Configuración

| Comando | Descripción |
|---|---|
| `/agregar <categoria> <ticker> <nombre> <bloque> <region>` | Añade ticker al radar |
| `/eliminar <ticker>` | Elimina ticker del radar |
| `/ayuda` | Muestra todos los comandos |

Ejemplo:
```
/agregar watchlist AAPL Apple Tecnología USA
/agregar portfolio MSFT Microsoft Tecnología USA
/eliminar AAPL
```

---

## Estructura del proyecto

```
market-radar-ai/
├── bot.py              # bot de Telegram, comandos y jobs periódicos
├── scheduler.py        # ejecución standalone sin bot
├── web.py              # dashboard web FastAPI (puerto 8589)
├── generate_csv.py     # descarga y calcula métricas por ticker
├── fetch_data.py       # wrappers de yfinance (datos, noticias, macro)
├── scoring.py          # score multi-factor de oportunidad
├── ai_analysis.py      # integración con Claude (Anthropic)
├── database.py         # acceso a SQLite
├── config.py           # variables de entorno
├── tickers.yaml        # tickers configurados
├── Dockerfile
├── docker-compose.yml  # servicios: init, bot, web, backup
├── requirements.txt
├── templates/          # plantillas Jinja2 del dashboard web
├── .github/
│   └── workflows/
│       └── docker-publish.yml  # CI/CD: build y push a GHCR en cada push a main
├── data/               # base de datos SQLite y credenciales (creado en runtime)
│   ├── radar.db
│   ├── credentials.json
│   ├── totp_secret.key
│   └── backups/        # snapshots automáticos diarios (últimos 7)
└── output/             # CSV con último snapshot (creado en runtime)
    └── precios_global.csv
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
8. send_message()          → envía reporte + alertas a Telegram
```

### Comprobación de alertas (cada hora)

```
1. get_active_alerts()     → obtiene alertas activas de BD (precio, drawdown, score)
2. yf.Ticker.history()     → precio actual por ticker
3. Lee CSV para alertas    → drawdown y score del último snapshot
4. Comprueba condición     → precio below/above, drawdown >, score <
5. Si se cumple:           → notifica por Telegram, desactiva alerta, guarda en alert_history
```

---

## Dashboard web

El `docker-compose.yml` incluye un segundo servicio (`market-radar-web`) que arranca el dashboard web en el puerto `8589`. Se despliega junto al bot automáticamente con `docker compose up -d`.

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
- **Reportes** — historial paginado de análisis Claude (10 por página)
- **Generar reporte** — lanza el pipeline completo desde el navegador (máx. 2 por minuto); muestra error si el pipeline falla
- **`/health`** — endpoint JSON con estado de la base de datos, existencia del CSV y timestamp UTC; útil para healthchecks externos

### Web Push y PWA

El dashboard es una Progressive Web App (PWA) instalable. Soporta notificaciones push nativas en el navegador sin necesidad de tener la pestaña abierta.

**Funcionamiento:**
1. Al visitar el dashboard, el navegador registra el Service Worker (`/sw.js`)
2. El usuario suscribe su navegador a las notificaciones push desde Ajustes
3. Las alertas de precio/drawdown/score/stop-loss se envían como push al navegador además de por Telegram
4. Compatible con Chrome, Firefox, Edge, Safari 16.4+ y como PWA instalada en Android/iOS

**Implementación sin dependencias externas:**
- Claves VAPID generadas con `cryptography` (`data/vapid_private.pem` + `data/vapid_public.pem`)
- Cifrado RFC 8291 (AES-128-GCM + ECDH efímero) implementado en `push_utils.py`
- Suscripciones persistidas en la tabla `push_subscriptions` de SQLite

> **Nota:** Los Service Workers solo funcionan en orígenes seguros (HTTPS). En `localhost` funciona sin TLS. En producción se necesita un reverse proxy con TLS (Cloudflare Tunnel, Caddy, nginx).

### Autenticación

El dashboard usa autenticación segura basada en credenciales almacenadas en `data/credentials.json` (bcrypt):

- **Primer acceso**: se fuerza un asistente de configuración para establecer usuario y contraseña, seguido de la configuración de 2FA TOTP (compatible con Google Authenticator, Authy, etc.)
- **2FA TOTP**: obligatorio tras el primer acceso; se puede deshabilitar desde `Ajustes → 2FA`
- **Bloqueo por IP**: tras 5 intentos fallidos, la IP queda bloqueada 15 minutos
- **CSRF**: token global en todos los formularios POST
- **Sesiones**: UUID por sesión, expiran en 30 días; se invalidan al hacer logout

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

## Ejecución standalone

Si quieres ejecutar un análisis sin el bot:

```bash
python scheduler.py
```

Requiere las mismas variables de entorno. Útil para pruebas o si prefieres gestionar el scheduling externamente (cron, etc.).
