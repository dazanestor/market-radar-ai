#!/bin/sh
mkdir -p /app/data/backups /app/output/.matplotlib
exec "$@"
