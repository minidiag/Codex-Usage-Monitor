#!/bin/sh
# Deliberately uses a separate, disposable database, never production tables.
set -eu
cd /opt/codex-usage-monitor
usage_test_image=${USAGE_IMAGE:-}
set -a
. ./deploy/.env
set +a
USAGE_IMAGE=${usage_test_image:-${USAGE_IMAGE:-codex-usage-monitor:0.5.0}}
export USAGE_IMAGE
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T db createdb -U usage_owner codex_usage_test
export TEST_DATABASE_URL="postgresql://usage_owner:${OWNER_DB_PASSWORD}@db:5432/codex_usage_test"
set +e
docker run --rm --network codex-usage-monitor_database --user 0:0 -e TEST_DATABASE_URL "${USAGE_IMAGE:-codex-usage-monitor:0.5.0}" \
    sh -c 'pip install --no-cache-dir --no-index --find-links=/wheels -r server/requirements-test.txt >/dev/null && python -m unittest discover -s server/tests -v'
test_status=$?
set -e
docker compose --env-file deploy/.env -f deploy/compose.yml exec -T db dropdb -U usage_owner codex_usage_test
exit "$test_status"
