#!/usr/bin/env bash
# Бэкап Postgres (Task 0019 + issue #81, FOUNDATION §10.10, §11):
# дамп → проверка архива → шифрование публичным ключом основателя → retention.
#
# Живёт на сервере как /opt/hospitality/backup.sh (кладёт deploy CI при каждом
# деплое) и запускается cron'ом ежедневно; строка crontab и восстановление —
# docs/runbooks/restore.md. Локально можно прогнать против ops/docker-compose.yml:
#   COMPOSE_FILE=ops/docker-compose.yml ENV_FILE=.env BACKUP_DIR=backups \
#   BACKUP_AGE_RECIPIENT=age1... ./ops/backup/backup.sh
#
# Первый аргумент — метка в имени файла; по умолчанию `hospitality` (ночной
# бэкап). С меткой `pre-migrate-<тег образа>` этот же скрипт зовёт deploy.sh
# перед `alembic upgrade` (issue #135): снимок перед миграцией обязан так же
# проверяться, шифроваться и стареть, а второй экземпляр этой логики разошёлся
# бы с каноном при первой же правке (P-12).
#
# Дамп снимается внутри контейнера db (канон deploy.sh: БД не открыта наружу,
# достать её можно только через docker) в формате custom (-Fc): сжат и
# восстанавливается pg_restore выборочно.
#
# Шифрование (issue #81, spec 0024): на сервере только ПУБЛИЧНЫЙ ключ age,
# приватный — на машине основателя и в его менеджере паролей. Сервер физически
# не может прочитать свои же бэкапы; расшифровка — шаг восстановления в
# restore.md. Нет ключа или нет age → бэкап падает и файла не создаёт
# (fail-closed: открытый дамп с перепиской гостей — это и есть issue #81).
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-/opt/hospitality/docker-compose.staging.yml}"
ENV_FILE="${ENV_FILE:-/opt/hospitality/.env}"
BACKUP_DIR="${BACKUP_DIR:-/opt/hospitality/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

# --- Метка в имени файла -----------------------------------------------------
# Метка идёт в путь файла, поэтому набор символов ограничен: пробел или слэш
# сломали бы путь, а имя с ведущим дефисом — разбор аргументов у find и rm
# (файл «-rf» в каталоге бэкапов). Негодная метка = стоп до дампа, а не файл
# со странным именем, который никто потом не найдёт при восстановлении.
BACKUP_LABEL="${1:-hospitality}"
case "$BACKUP_LABEL" in
    ""|[-.]*|*[!A-Za-z0-9._-]*)
        echo "ОШИБКА: негодная метка бэкапа «${BACKUP_LABEL}» — бэкап НЕ создан." >&2
        echo "Допустимы латиница, цифры, точка, дефис, подчёркивание; первый символ — буква или цифра." >&2
        exit 1
        ;;
esac

# Дампы и временные файлы — только для владельца (до issue #81 были 0664).
umask 077

script_dir="$(cd "$(dirname "$0")" && pwd)"

compose() {
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

env_value() {
    # Значение из ENV_FILE точечно, без source: в файле секреты (§11).
    # Канон — deploy.sh; последняя строка выигрывает, как и у compose.
    grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

# --- Чем шифруем -------------------------------------------------------------
# Порядок поиска: явный AGE_BIN → age в PATH → /usr/local/bin → бинарник в
# каталоге деплоя. Два последних пути — про cron: у него PATH=/usr/bin:/bin,
# поэтому ни бинарник из bootstrap-server.sh (/usr/local/bin/age), ни
# положенный вручную /opt/hospitality/bin/age иначе не нашлись бы, и ночной
# бэкап падал бы там, где ручная проверка по ssh проходит (PATH шире).
AGE_BIN="${AGE_BIN:-}"
if [ -z "$AGE_BIN" ]; then
    for candidate in "$(command -v age || true)" /usr/local/bin/age "$script_dir/bin/age"; do
        # -f обязателен: -x истинно и для каталога, а каталог не запустишь.
        if [ -n "$candidate" ] && [ -f "$candidate" ] && [ -x "$candidate" ]; then
            AGE_BIN="$candidate"
            break
        fi
    done
fi
if [ -z "$AGE_BIN" ] || [ ! -x "$AGE_BIN" ]; then
    echo "ОШИБКА: не найден age — шифровать бэкап нечем, бэкап НЕ создан." >&2
    echo "Установите age (docs/runbooks/restore.md, раздел «Шифрование»):" >&2
    echo "  сервер без root — бинарник в $script_dir/bin/age; новый сервер — bootstrap-server.sh." >&2
    exit 1
fi

# --- Кому шифруем ------------------------------------------------------------
BACKUP_AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:-$(env_value BACKUP_AGE_RECIPIENT)}"
if [ -z "$BACKUP_AGE_RECIPIENT" ]; then
    echo "ОШИБКА: не задан BACKUP_AGE_RECIPIENT — бэкап НЕ создан (issue #81)." >&2
    echo "Публичный ключ основателя (age1...) должен быть в $ENV_FILE:" >&2
    echo "  BACKUP_AGE_RECIPIENT=age1..." >&2
    echo "Как получить ключ — docs/runbooks/restore.md, раздел «Шифрование»." >&2
    exit 1
fi
case "$BACKUP_AGE_RECIPIENT" in
    AGE-SECRET-KEY-1*)
        echo "ОШИБКА: в BACKUP_AGE_RECIPIENT лежит ПРИВАТНЫЙ ключ age." >&2
        echo "Приватный ключ на сервере обнуляет смысл шифрования: уберите его из" >&2
        echo "$ENV_FILE, впишите публичный (age1...) и ротируйте пару ключей." >&2
        exit 1
        ;;
    age1*) ;;
    *)
        echo "ОШИБКА: BACKUP_AGE_RECIPIENT не похож на публичный ключ age (ожидается age1...)." >&2
        exit 1
        ;;
esac

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
plain="$BACKUP_DIR/$BACKUP_LABEL-$stamp.dump"
dump="$plain.age"

# Открытый дамп пишется под временным именем, шифртекст — тоже; боевое имя
# получает только зашифрованный и проверенный файл. Упавший pg_dump, битый
# архив или сбой шифрования не оставят файла, неотличимого от валидного бэкапа
# (его «свежайшим» забрал бы make backup-fetch в день аварии).
plain_part="$plain.part"
part="$dump.part"
trap 'rm -f "$plain_part" "$part"' EXIT

echo "==> [$stamp] pg_dump → $dump"
compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > "$plain_part"

# Битый или пустой архив должен обнаружиться сейчас, а не в день аварии
# (§10.10 «проверка восстановления» в минимальной автоматической форме).
if [ ! -s "$plain_part" ]; then
    echo "ОШИБКА: дамп пуст — бэкап НЕ создан: $dump" >&2
    exit 1
fi
# Без имени файла: pg_restore читает архив из stdin (путь /dev/stdin внутри
# docker exec не работает — проверено при обкатке Task 0019). Читаемость
# проверяется ДО шифрования: после него сервер уже не может прочитать архив —
# приватного ключа у него нет, и это правильно.
echo "==> Проверяю архив (pg_restore --list)..."
compose exec -T db pg_restore --list < "$plain_part" > /dev/null

echo "==> Шифрую для $BACKUP_AGE_RECIPIENT ($AGE_BIN)..."
"$AGE_BIN" --recipient "$BACKUP_AGE_RECIPIENT" --output "$part" "$plain_part"
if [ ! -s "$part" ]; then
    echo "ОШИБКА: шифрование дало пустой файл — бэкап НЕ создан: $dump" >&2
    exit 1
fi
# Заголовок формата age — дешёвая проверка, что в файле именно шифртекст.
if [ "$(head -c 21 "$part")" != "age-encryption.org/v1" ]; then
    echo "ОШИБКА: в файле нет заголовка age — бэкап НЕ создан: $dump" >&2
    exit 1
fi
# С этого момента открытого дампа на диске нет: он жил секунды и только с
# правами владельца. Расшифровать бэкап можно лишь приватным ключом основателя.
rm -f "$plain_part"
mv "$part" "$dump"

# Хвост ОТКРЫТОГО дампа от прерванного прогона (kill, ребут — trap не сработал)
# не должен ждать конца retention: это переписка гостей открытым текстом.
# Два часа — заведомо больше одного прогона, так что живой дамп не заденем.
find "$BACKUP_DIR" -maxdepth 1 -name '*.dump.part' -mmin +120 -print -delete

echo "==> Retention: удаляю дампы старше $BACKUP_RETENTION_DAYS дней..."
# *.part здесь — хвосты прерванных прогонов (kill/перезагрузка), до которых не
# дошёл trap; штатные провалы подчищаются сразу. Нешифрованные hospitality-*.dump
# из Task 0019 тоже подпадают под retention — старые бэкапы уходят по расписанию.
# Глоб — по расширению, а не по метке: retention это свойство КАТАЛОГА, а не
# прогона. Ночной дамп и снимок перед миграцией (issue #135) стареют по одним
# часам, и каждый прогон убирает всё старое; иначе ночной cron не трогал бы
# pre-migrate-дампы, а деплой — ночные, и чей-то мусор копился бы вечно.
find "$BACKUP_DIR" -maxdepth 1 \
    \( -name '*.dump.age' -o -name '*.dump.age.part' \
       -o -name '*.dump' -o -name '*.dump.part' \) \
    -mtime "+$BACKUP_RETENTION_DAYS" -print -delete

# Нешифрованные дампы старого формата: retention уберёт их сам через
# BACKUP_RETENTION_DAYS, но переписку гостей не стоит ждать две недели.
legacy_count="$(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' | wc -l | tr -d ' ')"
if [ "$legacy_count" != "0" ]; then
    echo "ВНИМАНИЕ: в $BACKUP_DIR осталось $legacy_count НЕшифрованных дампов (до issue #81)." >&2
    echo "Удалить: rm -f $BACKUP_DIR/*.dump" >&2
fi

echo "==> OK: бэкап создан, проверен и зашифрован: $dump ($(du -h "$dump" | cut -f1))"
