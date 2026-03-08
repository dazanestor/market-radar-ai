# Market Radar AI

Bot de Telegram para monitoreo de cartera e inversiones. Descarga datos de mercado, calcula métricas técnicas y fundamentales, puntúa oportunidades y genera análisis diarios con Claude (Anthropic).

---

## Tabla de contenidos

- [Características](#características)
- [Arquitectura](#arquitectura)
- [Requisitos](#requisitos)
- [Instalación y despliegue](#instalación-y-despliegue)
- [Configuración](#configuración)
- [Comandos del bot](#comandos-del-bot)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Base de datos](#base-de-datos)
- [Scoring](#scoring)
- [Flujo de datos](#flujo-de-datos)

---

## Características

- Monitoreo de cartera personal y watchlist de acciones
- Descarga de precios históricos, dividendos y fundamentales via yfinance
- Scoring multi-factor (drawdown, momentum, volatilidad, dividendos, ROE, PER)
- Análisis diario automatizado con Claude (Anthropic) incluyendo contexto macro
- Noticias recientes por ticker en el análisis
- Alertas automáticas de precio configurables
- Gráficos de precio e historial de drawdown enviados por Telegram
- Control completo desde Telegram: añadir/eliminar tickers, registrar posiciones, ver rebalanceo
- Historial persistente en SQLite
- Despliegue con Docker usando imagen pre-compilada de GitHub Container Registry (GHCR)
- CI/CD con GitHub Actions: build automático multi-arquitectura (amd64 + arm64) en cada push a `main`

---

## Arquitectura

```
tickers.yaml          ← tickers configurados (portfolio + watchlist)
     |
fetch_data.py         ← descarga datos de yfinance (precios, dividendos, fundamentales, noticias, macro)
     |
generate_csv.py       ← calcula métricas técnicas y fundamentales, exporta CSV
     |
scoring.py            ← puntúa cada activo con score multi-factor
     |
ai_analysis.py        ← genera análisis con Claude (Anthropic)
     |
bot.py                ← bot de Telegram: comandos, jobs periódicos, charts
     |
database.py           ← persistencia SQLite (historial, posiciones, alertas, reportes)
```

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

### 3. Crear directorios de datos

```bash
mkdir -p data output
```

### 4. Arrancar

```bash
docker compose up -d
```

Docker descargará automáticamente la imagen publicada en `ghcr.io/dazanestor/market-radar-ai:latest`. El bot arranca inmediatamente y queda escuchando. El reporte diario se ejecuta a las 08:00 (Europe/Madrid por defecto).

### Parar

```bash
docker compose down
```

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
- Los tickers son los símbolos de yfinance (`OR.PA` para Paris, `SAN.MC` para Madrid, etc.).

---

## Comandos del bot

El bot solo responde a mensajes del `TELEGRAM_CHAT_ID` configurado.

### Análisis

| Comando | Descripción |
|---|---|
| `/reporte` | Genera análisis completo con Claude ahora mismo (macro + cartera + watchlist + noticias) |
| `/cartera` | Muestra portfolio con precio, drawdown, momentum, volatilidad y P&L |
| `/watchlist` | Muestra watchlist ordenada por score con indicador de oportunidad (alta/media/baja) |
| `/rebalanceo` | Muestra peso actual vs objetivo de cada posición con acción recomendada |
| `/grafico <ticker>` | Envia chart de precio del último año con máximo de 52 semanas |
| `/fundamentos <ticker>` | Muestra PER, P/B, ROE, margen neto, deuda y noticias recientes de cualquier ticker |
| `/historial <ticker>` | Muestra evolución de drawdown y score de los últimos 30 días + gráfico |
| `/reportes` | Lista los últimos 5 análisis de Claude con fecha |

### Posiciones

| Comando | Descripción |
|---|---|
| `/posicion <ticker> <acciones> <precio>` | Registra o actualiza posición en cartera |
| `/eliminar_posicion <ticker>` | Elimina posición de la cartera |

Ejemplo:
```
/posicion V 10 220.50
/posicion MA 5 380.00
```

### Alertas de precio

| Comando | Descripción |
|---|---|
| `/alerta <ticker> <precio>` | Crea alerta. Si el precio objetivo es menor al actual → alerta al bajar. Si es mayor → alerta al subir |
| `/mis_alertas` | Lista todas las alertas activas con su ID |
| `/borrar_alerta <id>` | Desactiva una alerta por ID |

Ejemplo:
```
/alerta COST 800      ← avisa cuando Costco baje a 800
/alerta V 280         ← avisa cuando Visa suba a 280
```

Las alertas se comprueban cada hora automáticamente. Una vez disparada, se desactiva.

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
├── generate_csv.py     # descarga y calcula métricas por ticker
├── fetch_data.py       # wrappers de yfinance (datos, noticias, macro)
├── scoring.py          # score multi-factor de oportunidad
├── ai_analysis.py      # integración con Claude (Anthropic)
├── database.py         # acceso a SQLite
├── config.py           # variables de entorno
├── tickers.yaml        # tickers configurados
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .github/
│   └── workflows/
│       └── docker-publish.yml  # CI/CD: build y push a GHCR en cada push a main
├── data/               # base de datos SQLite (creado en runtime)
│   └── radar.db
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
| `avg_price` | REAL | Precio medio de compra |

Poblar con `/posicion` o directamente:
```sql
INSERT INTO portfolio (ticker, shares, avg_price) VALUES ('V', 10, 220.50);
```

### `price_history`
Snapshot diario de cada ticker. Se guarda una entrada por ticker por día.

| Campo | Descripción |
|---|---|
| `ticker` / `date` | Clave única |
| `price` | Precio de cierre |
| `drawdown_52w` | Caída desde máximo 52 semanas (%) |
| `momentum_3m` / `momentum_6m` | Rendimiento 3 y 6 meses (%) |
| `volatility` | Volatilidad anualizada (%) |
| `dividend_yield` | Rentabilidad por dividendo (%) |
| `score` / `opportunity` | Puntuación y nivel de oportunidad |

### `price_alerts`
Alertas de precio activas o disparadas.

### `reports`
Historial de análisis generados por Claude.

---

## Scoring

Cada activo recibe un score numérico calculado con 6 factores:

| Factor | Peso | Lógica |
|---|---|---|
| Drawdown 52s | 30% | Mayor caída desde máximo = más oportunidad |
| Momentum 3m inverso | 15% | Caída reciente puede indicar punto de entrada |
| Volatilidad | 15% | Menor volatilidad = negocio más predecible (cap en 30%) |
| Dividendo | 15% | Mayor rentabilidad por dividendo = más calidad |
| ROE | 15% | ROE alto indica negocio de calidad (cap en 30%) |
| PER | 10% | PER bajo indica mayor margen de seguridad (rango 0-60) |

Clasificación final:
- **ALTA**: score > 15
- **MEDIA**: score > 8
- **BAJA**: score <= 8

---

## Flujo de datos

### Reporte diario (08:00)

```
1. get_macro_context()     → S&P500, VIX, bono 10Y
2. generate()              → descarga precios, fundamentales y calcula métricas
3. score_watchlist()       → puntúa todos los activos
4. save_snapshot()         → guarda en price_history
5. get_news()              → últimas noticias por ticker
6. analyze()               → envía todo a Claude y obtiene análisis
7. save_report()           → guarda el análisis en BD
8. send_message()          → envía reporte + alertas a Telegram
```

### Comprobación de alertas (cada hora)

```
1. get_active_alerts()     → obtiene alertas activas de BD
2. yf.Ticker.history()     → precio actual por ticker
3. Comprueba condición     → below/above según dirección
4. Si se cumple:           → notifica por Telegram y desactiva alerta
```

---

## Ejecución standalone

Si quieres ejecutar un análisis sin el bot:

```bash
python scheduler.py
```

Requiere las mismas variables de entorno. Útil para pruebas o si prefieres gestionar el scheduling externamente (cron, etc.).
