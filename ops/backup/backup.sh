#!/usr/bin/env bash
# Бэкап Postgres (Task 0019, FOUNDATION §10.10): дамп + проверка архива + retention.
#
# Живёт на сервере как /opt/hospitality/backup.sh (кладёт deploy CI при каждом
# деплое) и запускается cron'ом ежедневно; строка crontab и восстановление —
# docs/runbooks/restore.md. Локально можно прогнать против ops/docker-compose.yml:
#   COMPOSE_FILE=ops/docker-compose.yml ENV_FILE=.env BACKUP_DIR=backups ./ops/backup/backup.sh
#
# Дамп снимается внутри контейнера db (канон deploy.sh: БД не открыта наружу,
# достать её можно только через docker) в формате custom (-Fc): сжат и
# восстанавливается pg_restore выборочно.
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/opt/hospitality/docker-compose.staging.yml}"
ENV_FILE="${ENV_FILE:-/opt/hospitality/.env}"
BACKUP_DIR="${BACKUP_DIR:-/opt/hospitality/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

compose() {
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="$BACKUP_DIR/hospitality-$stamp.dump"

echo "==> [$stamp] pg_dump → $dump"
compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$dump"

# Битый или пустой архив должен обнаружиться сейчас, а не в день аварии
# (§10.10 «проверка восстановления» в минимальной автоматической форме).
if [ ! -s "$dump" ]; then
    echo "ОШИБКА: дамп пуст — бэкап НЕ создан: $dump" >&2
    rm -f "$dump"
    exit 1
fi
# Без имени файла: pg_restore читает архив из stdin (путь /dev/stdin внутри
# docker exec не работает — проверено при обкатке Task 0019).
echo "==> Проверяю архив (pg_restore --list)..."
compose exec -T db pg_restore --list < "$dump" > /dev/null

echo "==> Retention: удаляю дампы старше $BACKUP_RETENTION_DAYS дней..."
find "$BACKUP_DIR" -name 'hospitality-*.dump' -mtime "+$BACKUP_RETENTION_DAYS" -print -delete

echo "==> OK: бэкап создан и проверен: $dump ($(du -h "$dump" | cut -f1))"
