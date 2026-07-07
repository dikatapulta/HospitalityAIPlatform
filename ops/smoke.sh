#!/usr/bin/env bash
# Smoke-тест Task 0004: среда поднимается, Postgres и Redis отвечают.
# Используется в CI (docker-compose job) и вручную: ./ops/smoke.sh
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE_FILE="ops/docker-compose.yml"
ENV_FILE=".env"
[ -f "$ENV_FILE" ] || ENV_FILE=".env.example"

compose() {
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

cleanup() {
    compose down --volumes
}
trap cleanup EXIT

echo "==> Поднимаю db и redis..."
compose up -d --wait --wait-timeout 60 db redis

echo "==> Проверяю Postgres..."
compose exec -T db pg_isready -U "$(grep -E '^POSTGRES_USER=' "$ENV_FILE" | cut -d= -f2)"

echo "==> Проверяю Redis..."
compose exec -T redis redis-cli ping

echo "==> OK: Postgres и Redis отвечают."
