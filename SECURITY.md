# SECURITY.md — Política de Seguridad y Privacidad

## Market Radar AI — Sistema de Gestión de Seguridad de la Información (SGSI)

**Estándar de referencia:** ISO/IEC 27001:2022 · ISO/IEC 27701:2019
**Versión:** 1.0 · **Fecha de revisión:** 2026-03-22
**Responsable:** Propietario del sistema (single-user, self-hosted)
**Alcance:** Aplicación web de monitoreo de cartera de inversiones. Single-user. Despliegue Docker en infraestructura propia.

---

## 1. POLÍTICA DE SEGURIDAD (ISO 27001 A.5)

### 1.1 Objetivos de seguridad

Esta aplicación implementa controles de seguridad para garantizar:

- **Confidencialidad**: Solo el usuario autorizado accede a los datos de cartera e inversiones.
- **Integridad**: Los datos no son alterados sin autorización. Backups con checksums SHA-256.
- **Disponibilidad**: Backups diarios cifrados. RTO ≤ 4 horas. RPO ≤ 24 horas.

### 1.2 Principios

- **Menor privilegio**: Aplicación ejecutada como usuario no-root (`appuser`) en Docker.
- **Defensa en profundidad**: Múltiples capas (2FA, CSRF, rate-limit, CSP, audit log).
- **Privacy by design**: Retención mínima de datos, exportación y borrado disponibles.
- **Transparencia**: Audit log de todos los eventos de seguridad, accesible al usuario.

### 1.3 Revisión anual

Esta política debe revisarse cada 12 meses o tras:
- Incidente de seguridad significativo
- Cambio de proveedor crítico (Anthropic, yfinance)
- Actualización mayor de dependencias

---

## 2. ANÁLISIS DE RIESGOS (ISO 27001 A.6 / A.8)

### 2.1 Inventario de activos

| Activo | Tipo | Clasificación | Propietario | Ubicación |
|--------|------|---------------|-------------|-----------|
| `data/radar.db` | Dato | **Confidencial** | Usuario | Volumen Docker local |
| `data/backups/` | Dato | **Confidencial** | Usuario | Volumen Docker local |
| `data/credentials.json` | Secreto | **Confidencial** | Usuario | Filesystem host (0o600) |
| `data/totp_secret.key` | Secreto | **Confidencial** | Usuario | Filesystem host (0o600) |
| Imagen Docker (`ghcr.io/...`) | Software | Público | Proyecto | GitHub Container Registry |
| API Key Anthropic | Secreto | **Confidencial** | Usuario | `.env` / tabla `settings` |
| Push subscriptions (endpoint, p256dh, auth) | Dato personal | **Personal** | Usuario | `push_subscriptions` en BD |
| Claves VAPID (privada) | Secreto | **Confidencial** | Sistema | `data/vapid_private.pem` (chmod 600) |
| Historial de precios | Dato de mercado | Público | Sistema | `price_history` en BD |

### 2.2 Matriz de riesgos

| # | Amenaza | Vulnerabilidad | Probabilidad | Impacto | Riesgo | Control mitigante | Riesgo residual |
|---|---------|----------------|:---:|:---:|:---:|---|:---:|
| R01 | Acceso no autorizado | Contraseña débil / sin 2FA | 3 | 5 | **Alto** | bcrypt + TOTP + bloqueo IP + expiración 90d | Bajo |
| R02 | Robo de BD SQLite | Acceso físico al servidor | 2 | 5 | **Alto** | Backups cifrados AES-256. BD sin cifrado adicional. | Medio |
| R03 | Inyección SQL | Queries no parametrizadas | 1 | 5 | **Medio** | Todos los queries usan placeholders `?` | Muy bajo |
| R04 | XSS / CSRF | Formularios web sin protección | 2 | 4 | **Medio** | CSP strict + CSRF token rotatorio + escape Jinja2 | Bajo |
| R05 | Fuga de API Key | `.env` expuesto en logs o repositorio | 2 | 4 | **Medio** | `.gitignore`, `.dockerignore`. Sin hardcode. | Bajo |
| R06 | Fallo de disponibilidad | Caída de servicio yfinance / Anthropic | 3 | 3 | **Medio** | Timeouts (120s Claude). Retries en fetch. Job de health check. | Bajo |
| R07 | Pérdida de datos | Disco lleno / corrupción SQLite | 2 | 4 | **Medio** | WAL mode. Backups diarios. integrity_check dominical. | Bajo |
| R08 | Sesión secuestrada | Cookie sin Secure flag (HTTP) | 2 | 4 | **Medio** | COOKIE_SECURE=1 en producción + HSTS. SameSite=strict. | Bajo (con HTTPS) |
| R09 | Transferencia datos a Anthropic | Datos de cartera en prompts Claude | 3 | 3 | **Medio** | DPA Anthropic referenciado. Datos no incluyen PII directa. | Medio |
| R10 | Ataques de fuerza bruta | Endpoint login sin límites | 1 | 4 | **Bajo** | Rate limit 3/min + bloqueo IP 15min (5 fallos) + bloqueo cuenta 30min (10 fallos multi-IP) + límite 3 intentos TOTP por sesión | Muy bajo |
| R11 | Backup sin cifrar accesible | BACKUP_PASSPHRASE no configurado | 3 | 4 | **Alto** | Advertencia en consola. Instruir configuración en producción. | Medio |
| R12 | Dependencias con CVE | Bibliotecas desactualizadas | 2 | 3 | **Medio** | pip-audit + Trivy en CI/CD (GitHub Actions) | Bajo |

**Escala de probabilidad:** 1=Improbable, 2=Posible, 3=Probable
**Escala de impacto:** 1=Mínimo, 5=Crítico

### 2.3 Acciones de mitigación pendientes

| Riesgo | Acción | Prioridad |
|--------|--------|-----------|
| R02 | Considerar SQLCipher o migración a PostgreSQL con cifrado | Media |
| R09 | Solicitar DPA formal a Anthropic para transferencias EU→USA | Alta |
| R11 | Configurar `BACKUP_PASSPHRASE` en producción | Inmediata |

---

## 3. CONTROLES DE ACCESO (ISO 27001 A.9)

### 3.1 Autenticación

| Control | Implementación | Referencia |
|---------|----------------|------------|
| Contraseña | bcrypt cost 12. Mínimo 10 chars, letras + números. | `web.py:_hash_password()` |
| 2FA TOTP | RFC 6238 con `pyotp`. QR en primer acceso. Secret validado como base32. | `web.py:_verify_totp()` |
| Expiración | 90 días. Aviso a 15 días. Forzado al expirar. | `web.py:_PASSWORD_EXPIRY_DAYS` |
| Bloqueo IP | 5 intentos fallidos → 15 min de bloqueo | `web.py:_LOCKOUT_MAX, _LOCKOUT_DURATION` |
| Bloqueo cuenta | 10 intentos fallidos (cualquier IP) → 30 min. Defiende contra rotación de IPs. | `web.py:_ACCOUNT_LOCKOUT_MAX, _ACCOUNT_LOCKOUT_DURATION` |
| Brute-force TOTP | Máx. 3 intentos TOTP fallidos por sesión pendiente → forzar re-login completo (A.9.2.2). | `web.py:_totp_failed_attempts, _TOTP_MAX_ATTEMPTS` |
| Cambio credenciales | Requiere contraseña actual. Rechaza sin verificación previa. | `web.py:credentials_update()` |
| Sesiones | UUID aleatorio 32B. TTL 30 días. Persistentes en BD. Restauradas tras reinicio. | `database.py:create_session_db()` |
| Sesiones concurrentes | Máximo 5 activas simultáneas. La más antigua se invalida al crear la nueva. | `web.py:_MAX_CONCURRENT_SESSIONS` |
| Session fixation | Nueva sesión generada tras cambio de credenciales (privilege change). | `web.py:credentials_update()` |
| CSRF | Token rotatorio 24h. Overlap 1h. `secrets.compare_digest()`. | `web.py:_validate_csrf()` |

### 3.2 Gestión de sesiones

- Sesiones almacenadas en tabla `sessions` (session_id, ip, user_agent, expires_at)
- `last_seen` actualizado en cada petición autenticada
- Limpieza automática de expiradas: `job_cleanup_sessions()` (diario a las 03:00)
- Invalidación inmediata en logout y tras borrado GDPR
- Límite de 5 sesiones concurrentes: la más antigua se rota automáticamente
- Cambio de credenciales requiere contraseña actual + invalida TODAS las sesiones activas (no solo la corriente) + crea nueva sesión (anti session fixation, ISO 27001 A.9.2.6)

### 3.3 Política de contraseñas

- Longitud mínima: 10 caracteres
- Requiere: al menos 1 letra y 1 número
- Expiración: 90 días desde último cambio
- Almacenamiento: bcrypt con salt aleatorio (cost 12, ~250ms/hash)
- Sin historial de contraseñas implementado (mejora futura)

---

## 4. CRIPTOGRAFÍA (ISO 27001 A.10)

### 4.1 Algoritmos aprobados

| Uso | Algoritmo | Clave | Implementación |
|-----|-----------|-------|----------------|
| Hash contraseñas | bcrypt | cost 12 | `bcrypt==4.2.1` |
| TOTP 2FA | HOTP-SHA1 (RFC 6238) | 160 bits | `pyotp==2.9.0` |
| Web Push payload | AES-128-GCM (RFC 8291) | 128 bits | `cryptography>=42.0.0` |
| Web Push VAPID JWT | ECDSA P-256 (RFC 8292) | 256 bits | `cryptography>=42.0.0` |
| Backups | AES-256-CBC + PBKDF2 | 256 bits | `openssl enc` |
| Sesiones | `secrets.token_urlsafe(32)` | 256 bits | stdlib Python |
| CSRF | `secrets.token_urlsafe(32)` | 256 bits | stdlib Python |

### 4.2 Gestión de claves

| Clave | Almacenamiento | Rotación |
|-------|----------------|----------|
| Contraseña de usuario | `data/credentials.json` (0o600, bcrypt) | Manual. Forzada cada 90 días. |
| TOTP secret | `data/totp_secret.key` (0o600) | Manual en `/2fa/setup` |
| API Key Anthropic | `.env` o tabla `settings` | Manual. Recomendar cada 90 días. |
| Claves VAPID | `data/vapid_private.pem` (chmod 600, excluido de imagen Docker) | Sin rotación automática (mejora futura) |
| BACKUP_PASSPHRASE | Variable de entorno `.env` | Manual |

### 4.3 Cifrado en tránsito

- **HTTPS requerido en producción**: reverse proxy (Cloudflare Tunnel, Caddy, nginx).
- `COOKIE_SECURE=1` activa `Secure` flag en cookies + cabecera HSTS.
- Web Push: cifrado extremo a extremo (AES-GCM) independiente de HTTPS.

### 4.4 Cifrado en reposo

- **Backups**: AES-256-CBC con PBKDF2 salt. Checksum SHA-256 adjunto.
- **BD SQLite** (`data/radar.db`): **sin cifrado nativo**. Riesgo R02.
  - Mitigación actual: permisos de filesystem + Docker network aislada.
  - Mejora recomendada: SQLCipher o full-disk encryption en el host.

---

## 5. LOGGING Y AUDITORÍA (ISO 27001 A.12.4)

### 5.1 Eventos auditados

| Evento | Descripción | Nivel |
|--------|-------------|-------|
| `login_success` | Login correcto — registra `uname_hash` | INFO |
| `login_failed` | Contraseña incorrecta — registra `uname_hash` | WARNING |
| `login_locked` | IP o cuenta bloqueada — registra `uname_hash` | WARNING |
| `logout` | Sesión cerrada | INFO |
| `totp_success` | Verificación 2FA correcta | INFO |
| `totp_failed` | Código 2FA incorrecto | WARNING |
| `totp_enabled` | 2FA activado | INFO |
| `totp_disabled` | 2FA desactivado | WARNING |
| `credentials_changed` | Cambio de usuario/contraseña — registra `uname_hash` | INFO |
| `credentials_change_rejected` | Contraseña actual incorrecta al intentar cambiar | WARNING |
| `password_expired` | Contraseña expirada en login — registra `uname_hash` | WARNING |
| `position_upserted` | Posición de cartera añadida o modificada | INFO |
| `position_deleted` | Posición de cartera eliminada | WARNING |
| `operation_added` | Operación buy/sell registrada | INFO |
| `operation_deleted` | Operación eliminada | WARNING |
| `ticker_added` | Ticker añadido a cartera/watchlist | INFO |
| `ticker_updated` | Metadatos de ticker actualizados | INFO |
| `ticker_deleted` | Ticker eliminado de cartera/watchlist | WARNING |
| `alert_created` | Alerta de precio/score/drawdown creada | INFO |
| `alert_deleted` | Alerta desactivada | INFO |
| `report_triggered` | Informe diario lanzado manualmente | INFO |
| `push_subscribed` | Suscripción Web Push registrada | INFO |
| `push_unsubscribed` | Suscripción Web Push eliminada | INFO |
| `unhandled_exception` | Excepción no capturada en un endpoint | ERROR |
| `gdpr_export` | Exportación de datos personales | INFO |
| `gdpr_delete` | Borrado de datos personales | WARNING |

> **Privacidad en logs**: ningún evento registra el nombre de usuario en texto plano. Se usa `uname_hash` = SHA-256(username) hexdigest truncado a 16 caracteres (ISO 27701 Art. 5 — minimización de datos).

### 5.2 Retención y purga

- Retención: **365 días**
- Purga automática: `purge_old_audit_log(days=365)` en `job_vacuum_db()` (domingos 02:00)
- Consulta: `GET /audit-log` (paginado, 50 eventos por página)

### 5.3 Monitoreo automático (ISO 27001 A.12.4.1)

`job_check_security_events()` (cada hora) analiza la ventana de la última hora:
- **≥5 login fallidos**: alerta Web Push
- **Evento crítico** (`gdpr_delete`, `totp_disabled`, `credentials_changed`): alerta inmediata
- **Integridad de BD**: `PRAGMA integrity_check` dominical. Alerta si falla.

---

## 6. GESTIÓN DE VULNERABILIDADES (ISO 27001 A.12.6 / A.14)

### 6.1 Escaneo en CI/CD (GitHub Actions)

Cada push a `main` ejecuta:

| Herramienta | Qué escanea | Artefacto |
|-------------|-------------|-----------|
| **Bandit** | Vulnerabilidades en código Python (SAST) | `bandit-report.json` (90 días) |
| **pip-audit** | CVEs en dependencias Python | `pip-audit-report.json` (90 días) |
| **Trivy** | Vulnerabilidades en imagen Docker | SARIF → GitHub Security tab |
| **anchore/sbom-action** | SBOM completo de la imagen (supply chain) | `sbom-spdx.json` (365 días, formato SPDX) |

### 6.2 Revisión manual periódica

- **Mensual**: revisar alertas Dependabot en GitHub.
- **Trimestral**: ejecutar `pip-audit` manualmente y actualizar dependencias.
- **Anual**: revisión completa de controles (ver sección 9).

---

## 7. GESTIÓN DE INCIDENTES (ISO 27001 A.16)

### 7.1 Definición de incidente de seguridad

- Acceso no autorizado sospechado (IPs desconocidas en audit_log)
- Más de 20 intentos de login fallidos en 24h desde distintas IPs
- Evento `gdpr_delete` no iniciado por el usuario
- Alerta de integridad de BD SQLite
- Exposición de `.env` o `credentials.json`
- Brecha en Anthropic que afecte datos enviados

### 7.2 Notificación de brecha de datos (Art. 33/34 RGPD — ISO 27001 A.16.1.5)

Si se confirma una brecha que afecta datos personales:

```
PLAZO MÁXIMO: 72 horas desde detección para notificar a la autoridad de control (AEPD).

PASO A: Evaluar gravedad (< 2h)
  ├── ¿Qué datos fueron expuestos? (portfolio, push endpoints, sesiones, audit_log)
  ├── ¿Cuántos registros? ¿Hay datos de identidad?
  ├── ¿Es acceso confirmado o solo potencial?
  └── ¿El acceso fue externo o interno?

PASO B: Notificar a la AEPD si hay alto riesgo (< 72h desde detección)
  └── Portal: https://sedeagpd.gob.es/sede-electronica-web/vistas/infoNotificacionViolacion/notificacionViolacion.jsf
  └── Datos a incluir: naturaleza, categorías, n.º de afectados, consecuencias, medidas adoptadas

PASO C: Notificar al usuario si hay alto riesgo individual (Art. 34)
  └── Template de notificación:
      "Estimado usuario: se ha detectado un acceso no autorizado a [descripción].
       Los datos potencialmente expuestos son: [lista].
       Acciones tomadas: [descripción].
       Recomendamos: cambiar contraseña, revisar alertas activas.
       Contacto: [email responsable]"

PASO D: Registrar en este fichero (sección 7.4)
```

### 7.3 Procedimiento de respuesta técnica

```
1. DETECCIÓN (< 1h)
   ├── Revisar /audit-log en el dashboard
   ├── Comprobar logs de Docker: docker logs market-radar-web
   └── Verificar integridad: docker exec market-radar-ai python3 -c
       "import sqlite3; c=sqlite3.connect('/app/data/radar.db'); print(c.execute('PRAGMA integrity_check').fetchone())"

2. CONTENCIÓN (< 2h)
   ├── Cambiar contraseña inmediatamente: /settings/credentials
   ├── Regenerar TOTP: /2fa/setup
   ├── Revocar API Key Anthropic en console.anthropic.com
   └── Detener servicio: docker compose down

3. ERRADICACIÓN (< 4h)
   ├── Identificar vector de ataque en logs
   ├── Aplicar parche o actualizar dependencias
   └── Restaurar desde backup si hay corrupción de datos

4. RECUPERACIÓN (< RTO = 4h)
   └── Ver sección 8 (Plan de Continuidad)

5. LECCIONES APRENDIDAS (< 7 días)
   ├── Documentar incidente (fecha, vector, impacto, acciones)
   ├── Actualizar matriz de riesgos si aplica
   └── Revisar controles afectados
```

### 7.4 Registro de incidentes / brechas

| Fecha | Tipo | Descripción | Datos afectados | AEPD notificada | Resolución |
|-------|------|-------------|-----------------|-----------------|------------|
| — | — | Sin incidentes registrados | — | — | — |

---

## 8. PLAN DE CONTINUIDAD (ISO 27001 A.17)

### 8.1 Objetivos de recuperación

| Métrica | Objetivo |
|---------|---------|
| **RTO** (Recovery Time Objective) | ≤ 4 horas |
| **RPO** (Recovery Point Objective) | ≤ 24 horas (frecuencia de backup diaria) |

### 8.2 Procedimiento de restauración desde backup cifrado

```bash
# 1. Verificar integridad del backup
sha256sum -c /path/to/radar_YYYYMMDD.db.enc.sha256

# 2. Descifrar el backup
openssl enc -d -aes-256-cbc -pbkdf2 \
  -in /path/to/radar_YYYYMMDD.db.enc \
  -out /tmp/radar_restored.db \
  -pass env:BACKUP_PASSPHRASE

# 3. Verificar la BD restaurada
python3 -c "
import sqlite3
conn = sqlite3.connect('/tmp/radar_restored.db')
result = conn.execute('PRAGMA integrity_check').fetchone()
print('Integridad:', result[0])
count = conn.execute('SELECT COUNT(*) FROM portfolio').fetchone()
print('Posiciones:', count[0])
"

# 4. Detener el servicio
docker compose down

# 5. Reemplazar la BD
cp /app/data/radar.db /app/data/radar.db.pre-restore.$(date +%Y%m%d)
cp /tmp/radar_restored.db /app/data/radar.db

# 6. Reiniciar el servicio
docker compose up -d

# 7. Verificar healthcheck
curl http://localhost:8589/health
```

### 8.3 Pruebas de recuperación

Se recomienda realizar una prueba de restauración **al menos una vez al año**:

```bash
# Test de restauración (no destructivo):
# 1. Seleccionar el backup más reciente
BACKUP=$(ls -t data/backups/radar_*.db.enc 2>/dev/null | head -1)
# 2. Restaurar en un directorio temporal
openssl enc -d -aes-256-cbc -pbkdf2 -in "$BACKUP" -out /tmp/radar_test.db -pass env:BACKUP_PASSPHRASE
# 3. Verificar integridad
python3 -c "import sqlite3; c=sqlite3.connect('/tmp/radar_test.db'); print(c.execute('PRAGMA integrity_check').fetchone()); print('Tickers:', c.execute('SELECT COUNT(*) FROM tickers').fetchone())"
# 4. Limpiar
rm /tmp/radar_test.db
```

**Registro de pruebas de restauración:**

| Fecha | Backup probado | Resultado | Duración | Observaciones |
|-------|----------------|-----------|----------|---------------|
| Pendiente | — | — | — | Programar prueba trimestral |

---

## 9. GESTIÓN DE CAMBIOS (ISO 27001 A.12.1)

### 9.1 Proceso de cambio

Para cambios en código, configuración o dependencias:

1. **Identificar** el cambio y su impacto en seguridad.
2. **Probar** en entorno local antes de desplegar.
3. **Revisar** dependencias afectadas con `pip-audit`.
4. **Documentar** en el commit message (convencional: `feat/fix/security/docs`).
5. **Desplegar** via GitHub Actions (CI/CD automático).
6. **Verificar** healthcheck post-despliegue.

### 9.2 Cambios críticos que requieren revisión adicional

- Cambios en autenticación (`web.py:_is_auth`, `_verify_password`, `_verify_totp`)
- Modificaciones en el esquema de BD (`database.py:init_db`)
- Actualizaciones de dependencias de seguridad (`bcrypt`, `cryptography`, `pyotp`)
- Cambios en `docker-compose.yml` que afecten redes o volúmenes

### 9.3 Versioning

El proyecto usa tags de Git para marcar versiones de producción:

```bash
# Crear tag de versión:
git tag -a v1.2.0 -m "descripción del cambio"
git push origin v1.2.0
```

---

## 10. CUMPLIMIENTO LEGAL (ISO 27001 A.18 / ISO 27701)

### 10.1 Marco legal aplicable

| Regulación | Aplicabilidad | Estado |
|------------|---------------|--------|
| RGPD (EU) 2016/679 | Sí — datos personales del usuario | Implementado |
| ISO/IEC 27001:2022 | Referencia. Sin certificación formal. | En implementación |
| ISO/IEC 27701:2019 | Referencia. Sin certificación formal. | En implementación |

### 10.2 Data Protection Impact Assessment (DPIA) — Transferencias a Anthropic

**Actividad de tratamiento:** Generación de análisis de cartera con Claude (Anthropic).

**Datos transferidos:**
- Tickers de cartera (símbolo bursátil, sector, región)
- Métricas de mercado (precio, drawdown, momentum, RSI)
- Noticias financieras públicas
- **NO se transfieren**: datos de identidad del usuario, importes exactos de inversión, ni información bancaria personal.

**Receptor:** Anthropic, Inc. (San Francisco, CA, USA).

**Base legal de transferencia:** Art. 46 RGPD — Cláusulas Contractuales Tipo (SCCs).

**Evaluación de riesgo:**
- Datos transferidos son predominantemente datos de mercado **públicos**.
- Identificación de la persona a través de tickers requiere contexto adicional del que Anthropic no dispone.
- Riesgo de re-identificación: **Bajo**.

**Acuerdo de procesamiento de datos:**
- DPA disponible en: [https://www.anthropic.com/legal/privacy](https://www.anthropic.com/legal/privacy)
- Mecanismo de transferencia: Cláusulas Contractuales Tipo (SCCs) — Module One (responsable → encargado)
- Sub-encargados de Anthropic: AWS (infraestructura de modelos)
- Retención en Anthropic: los datos de API no se usan para entrenar modelos (ver Privacy Policy)
- **Estado DPA:** Verificar y firmar DPA con Anthropic si se requiere compliance formal (contacto: privacy@anthropic.com).

### 10.3 Registro de actividades de tratamiento (Art. 30 RGPD)

| Actividad | Finalidad | Categorías de datos | Base jurídica | Retención | Destinatarios | Transferencias |
|-----------|-----------|---------------------|---------------|-----------|---------------|----------------|
| Gestión de cartera | Seguimiento de inversiones, alertas, rebalanceo | Tickers, participaciones, precios medios (no identificadores personales) | Interés legítimo | Hasta `/gdpr/delete` | Ninguno | No |
| Historial de operaciones | Registro buy/sell, cálculo P&L | Tickers, fechas, importes, notas | Interés legítimo | Hasta `/gdpr/delete` | Ninguno | No |
| Alertas de precio | Notificación de eventos de mercado | Tickers, precios objetivo | Interés legítimo | Hasta `/gdpr/delete` | Ninguno | No |
| Suscripciones push | Envío de notificaciones al navegador | Browser push endpoint, claves p256dh/auth | Interés legítimo | 90 días | Servicio push del navegador (Firefox/Chrome) — procesador | No |
| Sesiones de usuario | Autenticación y control de acceso | IP address, user-agent, session ID (hash) | Interés legítimo | 30 días | Ninguno | No |
| Registro de auditoría | Seguridad, detección de incidentes | IP address, tipo de evento, timestamp | Obligación legal (ISO 27001 A.12.4) | 365 días | Ninguno | No |
| Análisis de cartera con IA | Generación de informes de inversión | Tickers, métricas de mercado públicas (sin datos personales) | Interés legítimo | No retenido por Anthropic | Anthropic, Inc. (San Francisco, CA) — procesador | Sí — SCCs |

### 10.4 Retención de datos (ISO 27701 Art. 5)

| Datos | Retención | Mecanismo |
|-------|-----------|-----------|
| Historial de precios | 365 días | `purge_old_price_history()` — domingos |
| Caché de noticias | 30 días | `purge_old_news_cache()` — domingos |
| Registro de auditoría | 365 días | `purge_old_audit_log()` — domingos |
| Sesiones | 30 días | `job_cleanup_sessions()` — diario 03:00 |
| Suscripciones push | 90 días | `purge_old_push_subscriptions()` — domingos |
| Descubrimientos de mercado | 7 días | `purge_old_market_discoveries()` — domingos |
| Backups | 7 copias rotantes | Servicio `backup` — diario |
| Datos de cartera | Hasta borrado manual / GDPR | `/gdpr/delete` |

### 10.5 Derechos del interesado

| Derecho | Implementación | Ruta |
|---------|----------------|------|
| Acceso (Art. 15) | Exportación JSON completa | `GET /gdpr/export` |
| Rectificación (Art. 16) | Edición desde el dashboard | Dashboard general |
| Supresión (Art. 17) | Borrado con confirmación | `POST /gdpr/delete` |
| Portabilidad (Art. 20) | JSON portable con timestamp | `GET /gdpr/export` |

---

## 11. CHECKLIST DE AUDITORÍA INTERNA ANUAL

Ejecutar anualmente (o tras incidente significativo):

### A.9 Control de Acceso
- [ ] Verificar que la contraseña cumple la política (≥10 chars, expirada o próxima a expirar)
- [ ] Verificar que 2FA TOTP está activo
- [ ] Revisar sesiones activas en BD: `SELECT * FROM sessions WHERE expires_at > datetime('now')`
- [ ] Comprobar que bloqueo de IP funciona (5 intentos fallidos → 15 min)

### A.10 Criptografía
- [ ] Verificar que BACKUP_PASSPHRASE está configurado y backups son `.enc`
- [ ] Comprobar fecha de última rotación de TOTP secret (ver `/2fa/setup`)
- [ ] Verificar que API Key Anthropic no es pública (GitHub, logs)
- [ ] Test de restauración de backup (ver sección 8.3)

### A.12 Operaciones
- [ ] Revisar audit_log últimos 30 días en `/audit-log`
- [ ] Verificar que job_vacuum_db se ejecutó (domingos 02:00)
- [ ] Comprobar resultados de pip-audit en GitHub Actions (últimas 4 semanas)
- [ ] Revisar alertas Trivy en GitHub Security tab

### A.14 Desarrollo
- [ ] Verificar que los tests pasan: `python -m pytest tests/ -v`
- [ ] Revisar el último informe Bandit en GitHub Actions
- [ ] Comprobar que no hay secrets en el repositorio: `git log --all --full-history -- .env`

### A.17 Continuidad
- [ ] Realizar test de restauración de backup (documentar resultado arriba)
- [ ] Verificar que hay al menos 7 backups recientes en `data/backups/`
- [ ] Comprobar checksums: `sha256sum -c data/backups/radar_*.sha256`

### ISO 27701
- [ ] Revisar política de privacidad en `/privacy` (vigente y correcta)
- [ ] Verificar que el endpoint `/gdpr/export` genera el JSON correctamente (incluye push_subscriptions con endpoint_hash)
- [ ] Comprobar que la retención automática elimina datos según los plazos definidos (incluye market_discoveries ≤7d)
- [ ] Verificar que los mensajes Web Push de error no exponen detalles técnicos (tipo de excepción, paths, etc.)

### A.12 Disponibilidad — Resiliencia
- [ ] Verificar que `_fetch_price` con retry (`tenacity`) no genera warning excesivos en logs
- [ ] Comprobar que `entrypoint.sh` corrige permisos de `vapid_private.pem` en cada arranque
- [ ] Revisar que `_cleanup_expired_state()` está purgando `_failed_logins`, `_account_failed` y `_totp_failed_attempts` (sin acumulación indefinida)
- [ ] Comprobar en `/audit-log` que aparecen eventos `sessions_restored_from_db` tras reinicio del contenedor

---

## 12. DIVULGACIÓN RESPONSABLE DE VULNERABILIDADES

Si encuentras una vulnerabilidad de seguridad en este proyecto:

1. **No la hagas pública** hasta que el mantenedor pueda aplicar un parche.
2. Abre un [Security Advisory privado en GitHub](https://github.com/dazanestor/market-radar-ai/security/advisories/new).
3. Incluye: descripción, pasos para reproducir, impacto y versión afectada.
4. El mantenedor responderá en un plazo máximo de **7 días laborables**.

---

*Documento conforme a ISO/IEC 27001:2022 Annex A y ISO/IEC 27701:2019.*
*Revisión siguiente: 2027-03-22*
