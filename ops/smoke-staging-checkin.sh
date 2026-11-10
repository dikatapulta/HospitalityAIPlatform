#!/usr/bin/env bash
# Заселение smoke-комнаты на staging (spec 0074 §6): `tools/checkin` в контейнере
# app по SSH. Зовёт smoke-набор (SMOKE_CHECKIN_COMMAND, его задаёт
# ops/smoke-staging.sh) с аргументами CLI: `<комната> --guests 1` в начале прогона,
# `<комната> --check-out-now` в конце. Тенант — TELEGRAM_TENANT_SLUG контейнера,
# тот же, что у вебхука бота.
set -euo pipefail

cd "$(dirname "$0")/.."
# shellcheck source=ops/staging-ssh-lib.sh
source ops/staging-ssh-lib.sh

staging_ssh "cd /opt/hospitality && docker compose -f docker-compose.staging.yml --env-file .env exec -T app python -m hospitality.tools.checkin $(printf '%q ' "$@")"
