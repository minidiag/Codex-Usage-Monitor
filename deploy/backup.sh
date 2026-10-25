#!/bin/sh
set -eu
cd /opt/codex-usage-monitor
umask 077
install -d -m 700 backups
stamp=$(date -u +%Y%m%dT%H%M%SZ)
partial="backups/usage-${stamp}.dump.partial"
final="backups/usage-${stamp}.dump"
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T db pg_dump -U usage_owner -d codex_usage -Fc > "$partial"
test -s "$partial"
mv "$partial" "$final"
sha256sum "$final" > "${final}.sha256"
# Retention is restricted to files made by this script in this exact project directory.
find /opt/codex-usage-monitor/backups -maxdepth 1 -type f \( -name 'usage-*.dump' -o -name 'usage-*.dump.sha256' \) -mtime +7 -delete
printf 'Created local database backup: %s\n' "$final"
