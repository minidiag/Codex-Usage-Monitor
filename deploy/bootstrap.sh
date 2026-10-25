#!/bin/sh
set -eu
cd /opt/codex-usage-monitor
umask 077
python3 deploy/generate_env.py
set -a
. ./deploy/.env
set +a
docker compose --env-file deploy/.env -f deploy/compose.yml build api
docker compose --env-file deploy/.env -f deploy/compose.yml up -d db
attempt=0
until docker compose --env-file deploy/.env -f deploy/compose.yml exec -T db pg_isready -U usage_owner -d codex_usage >/dev/null 2>&1; do
    attempt=$((attempt+1))
    if [ "$attempt" -gt 30 ]; then printf 'Database startup timed out\n'; exit 1; fi
    sleep 2
done
export DATABASE_URL="postgresql://usage_owner:${OWNER_DB_PASSWORD}@db:5432/codex_usage"
docker compose --env-file deploy/.env -f deploy/compose.yml run --rm -T --no-deps -e DATABASE_URL api python -m server.admin migrate
docker compose --env-file deploy/.env -f deploy/compose.yml run --rm -T --no-deps -e DATABASE_URL -e APP_DB_PASSWORD api python -m server.admin app-role
install -d -m 700 private backups
if [ ! -f private/client.json ]; then
    docker compose --env-file deploy/.env -f deploy/compose.yml run --rm -T --no-deps --user 0:0 -e DATABASE_URL \
        -v /opt/codex-usage-monitor/private:/private api python -m server.admin provision --output /private/client.json
fi
docker compose --env-file deploy/.env -f deploy/compose.yml up -d api
attempt=0
until curl --connect-timeout 2 --max-time 3 -fsS http://127.0.0.1:8190/health/ready >/dev/null 2>&1; do
    attempt=$((attempt+1))
    if [ "$attempt" -gt 15 ]; then printf 'API readiness timed out\n'; exit 1; fi
    sleep 2
done
printf 'Private API started on 127.0.0.1:8190. Complete validation before enabling Nginx.\n'
