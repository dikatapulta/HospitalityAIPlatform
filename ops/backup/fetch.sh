#!/usr/bin/env bash
# Offsite-копия бэкапа (Task 0019, §10.11 bus factor = 1): забрать последний
# дамп Postgres со staging или гостевой копии на машину основателя. Пока сервер —
# единственная машина с данными, еженедельный `make backup-fetch` и
# `make backup-fetch-pilot` — та самая копия, которая переживёт потерю сервера
# (docs/runbooks/restore.md, docs/runbooks/pilot-copy.md).
#
# С issue #81 дампы зашифрованы (`*.dump.age`), и этот же прогон проверяет
# ключ основателя: расшифровывает скачанный файл и убеждается, что внутри
# настоящий дамп (магия PGDMP). Смысл — узнать «мой ключ не подходит» сегодня,
# а не в день аварии. Приватный ключ живёт только здесь, на сервере его нет.
set -euo pipefail
#
# Аргумент — чей бэкап (#391): `staging` (по умолчанию) или `pilot` — гостевая
# копия пилота на том же сервере (docs/runbooks/pilot-copy.md). Копии копии —
# живые гости, поэтому у них свой каталог вне репозитория и срок 14 суток
# (ops/backup/local-retention.sh); дампы staging — синтетика, лежат в backups/.
#
# С issue #1061 копию копии забирает ещё и агент launchd раз в час
# (ops/backup/fetch-agent.sh — его ставит этот же скрипт в конце `make
# backup-fetch-pilot`); агент зовёт свою копию скрипта со вторым аргументом
# `--agent`. Каждый прогон, убедившись, что дамп расшифровывается, ставит на сервере
# метку offsite-copy.stamp — её возраст сторожит алертер (ERR-OPS-023).

cd "$(dirname "$0")/../.."

usage() {
    echo "Использование: $0 [staging|pilot] [--agent]" >&2
    exit 2
}

agent=false
case "${2:-}" in
    "") ;;
    --agent) agent=true ;;
    *) usage ;;
esac

target_name="${1:-staging}"
case "$target_name" in
    staging)
        remote_root=/opt/hospitality
        local_dir=backups
        ;;
    pilot)
        remote_root=/home/deploy/hospitality-pilot
        # Вне ~/Desktop: агенту автоудаления macOS туда доступа не даёт.
        local_dir="${PILOT_BACKUP_LOCAL_DIR:-$HOME/necturn-pilot-backups}"
        mkdir -p -m 700 "$local_dir"
        local_dir="$(cd "$local_dir" && pwd)"
        # Сначала срок, потом сеть: старое удаляется, даже если сервер недоступен.
        ./ops/backup/local-retention.sh prune "$local_dir"
        # Агент агентов не ставит: их ставит прогон человека (иначе установка
        # из-под агента сняла бы его самого посреди забора).
        if [ "$agent" = false ]; then
            ./ops/backup/local-retention.sh install "$local_dir"
        fi
        ;;
    *) usage ;;
esac
remote_dir="$remote_root/backups"

if [ "$agent" = true ]; then
    echo "==> $(date -u '+%Y-%m-%dT%H:%M:%SZ') забор агентом"
    # У агента нет терминала: ssh не спрашивает пароль ключа и не ждёт сеть вечно.
    export STAGING_SSH_BATCH=1
fi

# shellcheck source=ops/staging-ssh-lib.sh
source ops/staging-ssh-lib.sh

# Приватный ключ age: переменная окружения → локальный .env → путь по умолчанию.
# Ключ у копии тот же, что у staging (pilot-copy.md, «Известные отличия»).
BACKUP_AGE_KEY="${BACKUP_AGE_KEY:-$(grep -E '^BACKUP_AGE_KEY=' .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true)}"
BACKUP_AGE_KEY="${BACKUP_AGE_KEY:-$HOME/.config/age/hospitality-backup.key}"
BACKUP_AGE_KEY="${BACKUP_AGE_KEY/#\~/$HOME}"

# Свежайший файл среди зашифрованных и (переходный период) нешифрованных дампов.
latest="$(staging_ssh "ls -1t ${remote_dir}/hospitality-*.dump.age ${remote_dir}/hospitality-*.dump 2>/dev/null | head -1")"
if [ -z "$latest" ]; then
    echo "На сервере нет ни одного дампа в ${remote_dir} —" >&2
    echo "бэкапы ещё не настроены или падают? См. docs/runbooks/restore.md," >&2
    echo "разделы «Расписание» и «Шифрование» (без ключа бэкап не создаётся)." >&2
    exit 1
fi

mkdir -p "$local_dir"

# Скачать файл сервера в каталог копий. Сначала во временное имя `.part`, боевое —
# после успеха: прерванный scp не оставит под боевым именем обрезанный файл, который
# следующий прогон счёл бы скачанным. Уже скачанный не качается заново — агент ходит
# раз в час, а дамп новый раз в сутки; проверка ключом ниже идёт каждый прогон.
fetch_file() {
    local target
    target="$local_dir/$(basename "$1")"
    if [ -f "$target" ]; then
        echo "==> Уже скачан: ${target}"
        return
    fi
    staging_scp_from "$1" "$target.part"
    mv "$target.part" "$target"
    echo "==> OK: ${target}"
}

fetch_file "$latest"
local_file="$local_dir/$(basename "$latest")"

# Архив тома файлов того же прогона (spec 0047): лежит рядом с той же меткой.
# Нет его — бэкап снят до spec 0047, это не ошибка; дамп важнее и уже скачан.
latest_files="${latest%.dump.age}.files.tar.age"
if [ "$latest_files" != "$latest" ] && staging_ssh "test -f '$latest_files'"; then
    fetch_file "$latest_files"
fi

case "$local_file" in
    *.dump.age) ;;
    *)
        # Открытых дампов на сервере не осталось (удалены 26.07 при внедрении
        # шифрования), поэтому нешифрованный свежайший файл = шифрование
        # отвалилось. Копия скачана, но это ошибка, а не «ну и ладно».
        echo "ОШИБКА: свежайший дамп на сервере НЕ зашифрован (issue #81)." >&2
        echo "Скачать его удалось, но шифрование на сервере не работает: проверь" >&2
        echo "BACKUP_AGE_RECIPIENT в ${remote_root}/.env, наличие age и хвост" >&2
        echo "${remote_dir}/backup.log." >&2
        exit 1
        ;;
esac

# Проверка ключа: расшифровываем в поток (открытый дамп на диск не попадает) и
# смотрим магию формата pg_dump -Fc. Нет age или ключа — это ошибка, а не
# пропуск: потерянный ключ и есть отказ, ради которого проверка написана (Д-8
# аудита #375).
age_bin="${AGE_BIN:-$(command -v age || true)}"
if [ -z "$age_bin" ]; then
    echo "ОШИБКА: нет age — расшифровку проверить нечем (установить: brew install age)." >&2
    exit 1
fi
if [ ! -f "$BACKUP_AGE_KEY" ]; then
    echo "ОШИБКА: нет приватного ключа $BACKUP_AGE_KEY — бэкап скачан, но" >&2
    echo "без ключа он не восстановим. Ключ — в менеджере паролей основателя;" >&2
    echo "куда положить — docs/runbooks/restore.md, «Шифрование»." >&2
    exit 1
fi

echo "==> Проверяю, что дамп расшифровывается ключом $BACKUP_AGE_KEY..."
# head берёт 5 байт, cat досасывает остальное — иначе age упал бы на закрытом
# пайпе и ошибка была бы не о том.
if ! magic="$("$age_bin" --decrypt --identity "$BACKUP_AGE_KEY" "$local_file" | { head -c 5; cat >/dev/null; })"; then
    echo "ОШИБКА: дамп не расшифровывается этим ключом." >&2
    echo "Либо на сервере в BACKUP_AGE_RECIPIENT чужой/старый публичный ключ," >&2
    echo "либо здесь не тот приватный. Нерасшифровываемый бэкап = отсутствие бэкапа." >&2
    exit 1
fi
if [ "$magic" != "PGDMP" ]; then
    echo "ОШИБКА: расшифровалось, но внутри не дамп pg_dump -Fc (нет магии PGDMP)." >&2
    exit 1
fi
echo "==> OK: бэкап расшифровывается ключом основателя и содержит дамп Postgres."

# Метка копии вне сервера (issue #1061) — только после проверки ключом выше: копия,
# которую не расшифровать, копией не считается. Время метки — время дампа (`touch
# -r`), а не забора: иначе ежечасный забор одного и того же старого дампа (ночной
# бэкап умер) держал бы её свежей. Возраст метки сторожит алертер — ERR-OPS-023.
staging_ssh "touch -r '$latest' '$remote_dir/offsite-copy.stamp'"
echo "==> OK: метка копии вне сервера обновлена (алерт ERR-OPS-023 о ней молчит)."

if [ "$target_name" = pilot ] && [ "$agent" = false ]; then
    STAGING_HOST="$STAGING_HOST" STAGING_SSH_USER="$STAGING_SSH_USER" \
        STAGING_SSH_KEY="$STAGING_SSH_KEY" BACKUP_AGE_KEY="$BACKUP_AGE_KEY" AGE_BIN="$age_bin" \
        ./ops/backup/fetch-agent.sh install "$local_dir"
fi
