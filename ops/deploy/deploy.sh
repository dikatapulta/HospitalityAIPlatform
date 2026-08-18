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

# --- Снимок БД ПЕРЕД миграцией (issue #135) ---------------------------------
# Упавшая или наполовину применённая миграция иначе лечится восстановлением из
# ночного бэкапа — это потеря до суток данных (RPO §10.10). Снимок стоит секунды
# и возвращает базу ровно в то состояние, из которого деплой начинался.
#
# Снимает его тот же скрипт, что и штатный бэкап (P-12: канон один —
# ops/backup/backup.sh), поэтому дамп так же проверяется `pg_restore --list`,
# шифруется публичным ключом основателя (issue #81) и стареет по той же
# retention-политике. Свежий backup.sh кладёт рядом с этим файлом scp-шаг
# деплоя — но только из CI: ручной ./deploy.sh на сервере (откат — deploy.md,
# «Часть C»; restore.md, случай В шаг 6) файлов не обновляет, так что рядом
# может лежать backup.sh любой давности. Отсюда проверка ниже: код возврата
# чужого скрипта — его слово, а обещаем мы файл на диске.
#
# Зовём через `bash <файл>`, а не как команду: бит исполняемости у scp'нутого
# файла зависит от версии OpenSSH на раннере, а деплой не должен от этого
# зависеть (интерпретатор тот же, что в shebang скрипта).
BACKUP_SCRIPT="./backup.sh"
# Каталог снимка: тот же, что получит backup.sh (переменную окружения уважаем —
# ею пользуются тесты и ручной прогон).
SNAPSHOT_DIR="${BACKUP_DIR:-$PWD/backups}"

# Тег образа в CI — git sha коммита; в имя дампа берём первые 12 символов: по
# ним видно, какой деплой снимок породил, а `ls` в каталоге бэкапов остаётся
# читаемым. APP_IMAGE приходит аргументом или из .env, то есть от человека,
# поэтому всё непригодное для имени файла заменяем дефисом: метку с такими
# символами backup.sh отвергнет, а деплой не должен вставать из-за имени.
image_tag="${APP_IMAGE##*/}"                       # последний сегмент: app:<sha>
case "$image_tag" in
    *:*) image_tag="${image_tag##*:}" ;;
    *) image_tag="" ;;                             # ссылка без тега
esac
image_tag="$(printf '%s' "$image_tag" | tr -c 'A-Za-z0-9._-' '-' | cut -c1-12)"
[ -n "$image_tag" ] || image_tag="unknown"

if [ "${SKIP_PRE_MIGRATE_BACKUP:-}" = "1" ]; then
    # Аварийный рычаг: чинить сломанный бэкап посреди инцидента бывает дороже,
    # чем выкатить фикс без страховки. Осознанное решение человека, поэтому —
    # переменная окружения на один прогон и громкая строка в логе (deploy.md).
    echo "ВНИМАНИЕ: снимок БД перед миграцией ПРОПУЩЕН (SKIP_PRE_MIGRATE_BACKUP=1)." >&2
    echo "Упавшая миграция откатится только из ночного бэкапа — до суток данных." >&2
else
    echo "==> Снимок БД перед миграцией: метка pre-migrate-$image_tag"
    if [ ! -f "$BACKUP_SCRIPT" ]; then
        echo "ОШИБКА: рядом с deploy.sh нет $BACKUP_SCRIPT — снимок БД перед миграцией" >&2
        echo "снять нечем, деплой остановлен (issue #135). Файл кладёт scp-шаг деплоя;" >&2
        echo "вручную: scp ops/backup/backup.sh deploy@<IP>:/opt/hospitality/" >&2
        exit 1
    fi

    # Причина стопа одна на оба отказа ниже, поэтому шапка сообщения общая.
    snapshot_failed() {
        echo "ОШИБКА: снимок БД не снят — миграции НЕ применялись, деплой остановлен." >&2
        echo "Мигрировать без страховки нельзя: упавшая миграция откатывалась бы из" >&2
        echo "ночного бэкапа с потерей до суток данных (issue #135)." >&2
    }

    # Пустой файл-маркер со временем «до дампа»: снимком ЭТОГО прогона считается
    # только файл свежее маркера. Проверять «файл с таким именем есть» нельзя —
    # второй деплой того же тега нашёл бы снимок первого и промолчал именно там,
    # где обязан кричать. umask 077 — как в backup.sh: в каталоге лежит
    # переписка гостей, и права на него ставит тот, кто его создал.
    SNAPSHOT_MARKER="$SNAPSHOT_DIR/.pre-migrate.marker"
    if ! (umask 077 && mkdir -p "$SNAPSHOT_DIR" && : > "$SNAPSHOT_MARKER"); then
        snapshot_failed
        echo "Каталог снимка $SNAPSHOT_DIR не готов: нет прав или места на диске." >&2
        exit 1
    fi
    trap 'rm -f "$SNAPSHOT_MARKER"' EXIT

    if ! COMPOSE_FILE="$PWD/$COMPOSE_FILE" ENV_FILE="$PWD/$ENV_FILE" \
         BACKUP_DIR="$SNAPSHOT_DIR" \
         bash "$BACKUP_SCRIPT" "pre-migrate-$image_tag"; then
        snapshot_failed
        echo "Частая причина — нет ключа BACKUP_AGE_RECIPIENT или бинарника age:" >&2
        echo "docs/runbooks/restore.md, «Шифрование». Аварийный обход (данные под" >&2
        echo "риском) — SKIP_PRE_MIGRATE_BACKUP=1: docs/runbooks/deploy.md, часть B." >&2
        exit 1
    fi

    # Ноль от backup.sh — не доказательство снимка. Устаревший backup.sh (до
    # issue #135) метку игнорирует и кладёт дамп под именем ночного бэкапа:
    # деплой формально прошёл бы, но `ls backups/pre-migrate-*` из restore.md
    # (случай В, шаг 3) не нашёл бы ничего, а будущий алерт #106 увидел бы
    # свежий hospitality-* и промолчал о мёртвом cron. Обрезанный при копировании
    # файл дал бы то же самое вообще без дампа. Поэтому обещание шага проверяется
    # тем, чем оно и было дано, — файлом на диске.
    if [ -z "$(find "$SNAPSHOT_DIR" -maxdepth 1 \
                    -name "pre-migrate-$image_tag-*.dump.age" \
                    -newer "$SNAPSHOT_MARKER" || true)" ]; then
        snapshot_failed
        echo "backup.sh завершился без ошибки, но снимка pre-migrate-$image_tag-*.dump.age" >&2
        echo "в $SNAPSHOT_DIR не появилось. Частая причина — устаревший backup.sh на" >&2
        echo "сервере (до issue #135): он игнорирует метку и кладёт дамп под именем" >&2
        echo "ночного бэкапа. Обновить: scp ops/backup/backup.sh deploy@<IP>:/opt/hospitality/" >&2
        exit 1
    fi
fi

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

# Регистрация вебхука Telegram на постоянный вход (issue #65). До этого шага
# вебхук ставился руками через временный туннель со случайным адресом и терялся
# при перезапуске; теперь адрес постоянный (staging.necturn.com через
# cloudflared) и setWebhook гоняется на каждом деплое — вход не «уезжает».
# Токен и секрет читаем из .env точечно (не source: в файле секреты, §11).
env_value() { grep -E "^$1=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true; }
BOT_TOKEN="$(env_value TELEGRAM_BOT_TOKEN)"
WEBHOOK_SECRET="$(env_value TELEGRAM_WEBHOOK_SECRET)"
PUBLIC_BASE_URL="$(env_value PUBLIC_BASE_URL)"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://staging.necturn.com}"

if [ -n "$BOT_TOKEN" ] && [ -n "$WEBHOOK_SECRET" ]; then
    WEBHOOK_URL="$PUBLIC_BASE_URL/channels/telegram/webhook"
    echo "==> Регистрирую вебхук Telegram: $WEBHOOK_URL"
    # secret_token — тот же TELEGRAM_WEBHOOK_SECRET, что проверяет app (router.py,
    # fail-closed). URL не логируем целиком: в нём токен бота (§11).
    curl -fsS --max-time 20 "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
        --data-urlencode "url=${WEBHOOK_URL}" \
        --data-urlencode "secret_token=${WEBHOOK_SECRET}" \
        --data-urlencode "allowed_updates=[\"message\",\"callback_query\"]" >/dev/null

    # Smoke входа: вебхук реально зарегистрирован на нашем адресе и без ошибки
    # доставки. Ловит обрыв входа (мёртвый туннель), которого не видел make smoke.
    INFO="$(curl -fsS --max-time 20 "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo")"
    case "$INFO" in
        *"$WEBHOOK_URL"*) echo "==> Вебхук зарегистрирован." ;;
        *) echo "ОШИБКА: getWebhookInfo не подтвердил $WEBHOOK_URL" >&2; echo "$INFO" >&2; exit 1 ;;
    esac
    # allowed_updates без callback_query = inline-кнопки молча не доезжают
    # (нажатие крутится и ничего не делает), при этом текст и команды работают.
    case "$INFO" in
        *callback_query*) : ;;
        *) echo "ОШИБКА: вебхук не подписан на callback_query — кнопки работать не будут" >&2; exit 1 ;;
    esac
else
    echo "==> Пропускаю setWebhook: TELEGRAM_BOT_TOKEN/WEBHOOK_SECRET не заданы (канал выключен)."
fi

# Бэкапы шифруются публичным ключом основателя (issue #81) и без него не
# создаются вовсе. С issue #135 негодный ключ обычно валит деплой раньше — на
# снимке перед миграцией; сюда доходим, когда снимок пропущен рычагом
# SKIP_PRE_MIGRATE_BACKUP. Тогда молчать тем более нельзя: ночные бэкапы не
# создаются, и «бэкапов нет» обнаружилось бы в день аварии.
# Проверяем не «непусто», а «похоже на ключ»: в .env.staging.example стоит
# непустой плейсхолдер ЗАМЕНИ_НА_..., и проверка на пустоту его пропустила бы.
case "$(env_value BACKUP_AGE_RECIPIENT)" in
    age1*) ;;
    *)
        echo "ВНИМАНИЕ: в .env нет публичного ключа BACKUP_AGE_RECIPIENT (age1...) —" >&2
        echo "БЭКАПЫ НЕ СОЗДАЮТСЯ (issue #81). Как исправить: docs/runbooks/restore.md, «Шифрование»." >&2
        ;;
esac

echo "==> Чищу старые неиспользуемые образы..."
docker image prune -f >/dev/null || true

echo "==> OK: staging здоров на образе $APP_IMAGE"
