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

cd "$(dirname "$0")/../.."

case "${1:-staging}" in
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
        ./ops/backup/local-retention.sh install "$local_dir"
        ;;
    *)
        echo "Использование: $0 [staging|pilot]" >&2
        exit 2
        ;;
esac
remote_dir="$remote_root/backups"

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
staging_scp_from "$latest" "$local_dir/"
local_file="$local_dir/$(basename "$latest")"
echo "==> OK: $local_file"

# Архив тома файлов того же прогона (spec 0047): лежит рядом с той же меткой.
# Нет его — бэкап снят до spec 0047, это не ошибка; дамп важнее и уже скачан.
latest_files="${latest%.dump.age}.files.tar.age"
if [ "$latest_files" != "$latest" ] && staging_ssh "test -f '$latest_files'"; then
    staging_scp_from "$latest_files" "$local_dir/"
    echo "==> OK: $local_dir/$(basename "$latest_files")"
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
