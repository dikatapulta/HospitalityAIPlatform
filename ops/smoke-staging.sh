#!/usr/bin/env bash
# Smoke против staging (Task 0019, spec 0019): тот же pytest-набор, что
# `make smoke`, но секреты среды подтягиваются по SSH с сервера и на диск
# не пишутся. Запуск: make smoke-staging (нужен STAGING_HOST в .env).
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=ops/staging-ssh-lib.sh
source ops/staging-ssh-lib.sh

echo "==> Подтягиваю секреты smoke с $STAGING_SSH_USER@$STAGING_HOST (в файлы не сохраняются)..."
secrets="$(staging_ssh "grep -E '^(TELEGRAM_WEBHOOK_SECRET|SERVICE_TOKEN)=' /opt/hospitality/.env")"
webhook_secret="$(echo "$secrets" | grep '^TELEGRAM_WEBHOOK_SECRET=' | cut -d= -f2-)"
service_token="$(echo "$secrets" | grep '^SERVICE_TOKEN=' | cut -d= -f2-)"

echo "==> Гоняю smoke против http://$STAGING_HOST:8000 ..."
SMOKE_BASE_URL="http://$STAGING_HOST:8000" \
SMOKE_WEBHOOK_SECRET="$webhook_secret" \
SMOKE_SERVICE_TOKEN="$service_token" \
    .venv/bin/pytest tests/smoke -m smoke --no-cov --tb=no --no-header -rN -p no:warnings
