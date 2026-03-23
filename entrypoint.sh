#!/bin/sh
set -e
mkdir -p /app/data/backups /app/output/.matplotlib
# ISO 27001 A.10.1.2: garantizar permisos correctos en clave VAPID privada al arrancar
if [ -f /app/data/vapid_private.pem ]; then
    chmod 600 /app/data/vapid_private.pem 2>/dev/null || true
fi
exec "$@"
