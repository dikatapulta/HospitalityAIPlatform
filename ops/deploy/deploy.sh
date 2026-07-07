#!/usr/bin/env bash
# Деплой staging (Task 0006). Запускается НА сервере — из CI по SSH или вручную.
# Идемпотентен: тянет образ APP_IMAGE из GHCR и перезапускает стек до готовности.
#
#   Из CI:   ./deploy.sh ghcr.io/<owner>/hospitality-app:<sha>
#   Повтор:  ./deploy.sh                 # берёт последний образ из .app_image
#   Откат:   ./deploy.sh ghcr.io/<owner>/hospitality-app:<старый-sha>
#
# Требует рядом с собой: docker-compose.staging.yml и .env (секреты, §11).
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE_FILE="docker-compose.staging.yml"
ENV_FILE=".env"
IMAGE_STATE=".app_image"  # последний успешно выкаченный образ — для повтора/отката

[ -f "$ENV_FILE" ] || {
    echo "ОШИБКА: нет $ENV_FILE рядом с deploy.sh. Скопируйте .env.staging.example → .env и заполните (docs/runbooks/secrets.md)." >&2
    exit 1
}

# Какой образ катим: аргумент → переменная окружения → последний известный.
APP_IMAGE="${1:-${APP_IMAGE:-}}"
if [ -z "$APP_IMAGE" ] && [ -f "$IMAGE_STATE" ]; then
    APP_IMAGE="$(cat "$IMAGE_STATE")"
fi
if [ -z "$APP_IMAGE" ]; then
    echo "ОШИБКА: не указан образ. Передайте APP_IMAGE аргументом (ghcr.io/.../hospitality-app:<sha>)." >&2
    exit 1
fi
export APP_IMAGE

compose() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

echo "==> Деплой образа: $APP_IMAGE"
compose pull

echo "==> Поднимаю стек и жду готовности (up --wait)..."
compose up -d --wait --wait-timeout 120

echo "==> Post-deploy smoke: /health/ready (Postgres + Redis)..."
compose exec -T app python -c '
import sys, urllib.request
try:
    resp = urllib.request.urlopen("http://localhost:8000/health/ready", timeout=5)
    sys.exit(0 if resp.status == 200 else 1)
except Exception as exc:  # HTTPError(503) при недоступной зависимости и пр.
    print("health/ready недоступен:", exc)
    sys.exit(1)
'

# Запоминаем образ только после успешного smoke — чтобы повтор поднимал рабочую версию.
echo "$APP_IMAGE" > "$IMAGE_STATE"

echo "==> Чищу старые неиспользуемые образы..."
docker image prune -f >/dev/null || true

echo "==> OK: staging здоров на образе $APP_IMAGE"
