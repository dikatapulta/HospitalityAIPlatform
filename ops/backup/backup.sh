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

# Дамп пишется под временным именем и получает боевое только после проверки
# архива: упавший pg_dump или битый архив не оставит файла, неотличимого от
# валидного бэкапа (его «свежайшим» забрал бы make backup-fetch в день аварии).
part="$dump.part"
trap 'rm -f "$part"' EXIT

echo "==> [$stamp] pg_dump → $dump"
compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$part"

# Битый или пустой архив должен обнаружиться сейчас, а не в день аварии
# (§10.10 «проверка восстановления» в минимальной автоматической форме).
if [ ! -s "$part" ]; then
    echo "ОШИБКА: дамп пуст — бэкап НЕ создан: $dump" >&2
    exit 1
fi
# Без имени файла: pg_restore читает архив из stdin (путь /dev/stdin внутри
# docker exec не работает — проверено при обкатке Task 0019).
echo "==> Проверяю архив (pg_restore --list)..."
compose exec -T db pg_restore --list < "$part" > /dev/null
mv "$part" "$dump"

echo "==> Retention: удаляю дампы старше $BACKUP_RETENTION_DAYS дней..."
# *.dump.part здесь — хвосты прерванных прогонов (kill/перезагрузка), до
# которых не дошёл trap; штатные провалы подчищаются сразу.
find "$BACKUP_DIR" \( -name 'hospitality-*.dump' -o -name 'hospitality-*.dump.part' \) \
    -mtime "+$BACKUP_RETENTION_DAYS" -print -delete

echo "==> OK: бэкап создан и проверен: $dump ($(du -h "$dump" | cut -f1))"
