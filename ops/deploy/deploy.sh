#!/usr/bin/env bash
# Деплой staging (Task 0006). Запускается НА сервере — из CI по SSH или вручную.
# Идемпотентен: тянет образ APP_IMAGE из GHCR и перезапускает стек до готовности.
#
#   Из CI:   ./deploy.sh ghcr.io/<owner>/hospitality-app:<sha>
#   Повтор:  ./deploy.sh                 # берёт последний образ из APP_IMAGE в .env
#   Откат:   ./deploy.sh ghcr.io/<owner>/hospitality-app:<старый-sha>
#
# Требует рядом с собой: docker-compose.staging.yml и .env (секреты, §11).
set -euo pipefail

cd "$(dirname "$0")"

COMPOSE_FILE="docker-compose.staging.yml"
ENV_FILE=".env"
LEGACY_IMAGE_STATE=".app_image"  # до Task 0007 образ хранился здесь, теперь — в .env

[ -f "$ENV_FILE" ] || {
    echo "ОШИБКА: нет $ENV_FILE рядом с deploy.sh. Скопируйте .env.staging.example → .env и заполните (docs/runbooks/secrets.md)." >&2
    exit 1
}

# Какой образ катим: аргумент → переменная окружения → последний задеплоенный.
APP_IMAGE="${1:-${APP_IMAGE:-}}"
if [ -z "$APP_IMAGE" ]; then
    APP_IMAGE="$(grep -E '^APP_IMAGE=' "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
fi
if [ -z "$APP_IMAGE" ] && [ -f "$LEGACY_IMAGE_STATE" ]; then
    APP_IMAGE="$(cat "$LEGACY_IMAGE_STATE")"
fi
if [ -z "$APP_IMAGE" ]; then
    echo "ОШИБКА: не указан образ. Передайте APP_IMAGE аргументом (ghcr.io/.../hospitality-app:<sha>)." >&2
    exit 1
fi
export APP_IMAGE

compose() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

echo "==> Деплой образа: $APP_IMAGE"
compose pull

# Миграции — ДО перезапуска приложения (Task 0009): новый код может требовать
# новую схему. Гоняются новым образом в одноразовом контейнере (БД staging не
# открыта наружу — только так до неё можно достать). Старое приложение в этот
# момент ещё работает, поэтому миграции обязаны быть обратно-совместимыми в
# рамках одного деплоя.
echo "==> Применяю миграции БД (alembic upgrade head)..."
compose up -d --wait --wait-timeout 120 db
compose run --rm --no-deps app alembic upgrade head

# Демо-данные (Task 0011/0013): тенант Demo Hotel + его категории заявок.
# Идемпотентно — существующие тенант, конфиг и категории сид не трогает,
# поэтому безопасен на каждом деплое.
echo "==> Сид демо-данных Demo Hotel (идемпотентно)..."
compose run --rm --no-deps app python -m hospitality.tools.seed

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

# Запоминаем образ в .env только после успешного smoke: повторный ./deploy.sh
# поднимает последнюю здоровую версию, а ручные `docker compose ... logs/ps`
# из runbook'ов работают без «APP_IMAGE не задан» — compose сам читает .env
# рядом с compose-файлом. cp -p сохраняет права файла (в .env секреты, §11).
cp -p "$ENV_FILE" "$ENV_FILE.new"
{ grep -vE '^APP_IMAGE=' "$ENV_FILE" || true; printf 'APP_IMAGE=%s\n' "$APP_IMAGE"; } > "$ENV_FILE.new"
mv "$ENV_FILE.new" "$ENV_FILE"
rm -f "$LEGACY_IMAGE_STATE"

echo "==> Чищу старые неиспользуемые образы..."
docker image prune -f >/dev/null || true

echo "==> OK: staging здоров на образе $APP_IMAGE"
