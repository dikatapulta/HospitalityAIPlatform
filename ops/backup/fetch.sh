#!/usr/bin/env bash
# Offsite-копия бэкапа (Task 0019, §10.11 bus factor = 1): забрать последний
# дамп Postgres со staging на машину основателя. Пока сервер — единственная
# машина с данными, еженедельный запуск `make backup-fetch` — та самая копия,
# которая переживёт потерю сервера (docs/runbooks/restore.md).
#
# С issue #81 дампы зашифрованы (`*.dump.age`), и этот же прогон проверяет
# ключ основателя: расшифровывает скачанный файл и убеждается, что внутри
# настоящий дамп (магия PGDMP). Смысл — узнать «мой ключ не подходит» сегодня,
# а не в день аварии. Приватный ключ живёт только здесь, на сервере его нет.
set -euo pipefail

cd "$(dirname "$0")/../.."
# shellcheck source=ops/staging-ssh-lib.sh
source ops/staging-ssh-lib.sh

# Приватный ключ age: переменная окружения → локальный .env → путь по умолчанию.
BACKUP_AGE_KEY="${BACKUP_AGE_KEY:-$(grep -E '^BACKUP_AGE_KEY=' .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true)}"
BACKUP_AGE_KEY="${BACKUP_AGE_KEY:-$HOME/.config/age/hospitality-backup.key}"
BACKUP_AGE_KEY="${BACKUP_AGE_KEY/#\~/$HOME}"

# Свежайший файл среди зашифрованных и (переходный период) нешифрованных дампов.
latest="$(staging_ssh "ls -1t /opt/hospitality/backups/hospitality-*.dump.age /opt/hospitality/backups/hospitality-*.dump 2>/dev/null | head -1")"
if [ -z "$latest" ]; then
    echo "На сервере нет ни одного дампа в /opt/hospitality/backups —" >&2
    echo "бэкапы ещё не настроены или падают? См. docs/runbooks/restore.md," >&2
    echo "разделы «Расписание» и «Шифрование» (без ключа бэкап не создаётся)." >&2
    exit 1
fi

mkdir -p backups
staging_scp_from "$latest" backups/
local_file="backups/$(basename "$latest")"
echo "==> OK: $local_file"

case "$local_file" in
    *.dump.age) ;;
    *)
        # Открытых дампов на сервере не осталось (удалены 26.07 при внедрении
        # шифрования), поэтому нешифрованный свежайший файл = шифрование
        # отвалилось. Копия скачана, но это ошибка, а не «ну и ладно».
        echo "ОШИБКА: свежайший дамп на сервере НЕ зашифрован (issue #81)." >&2
        echo "Скачать его удалось, но шифрование на сервере не работает: проверь" >&2
        echo "BACKUP_AGE_RECIPIENT в /opt/hospitality/.env, наличие age и хвост" >&2
        echo "/opt/hospitality/backups/backup.log." >&2
        exit 1
        ;;
esac

# Проверка ключа: расшифровываем в поток (открытый дамп на диск не попадает) и
# смотрим магию формата pg_dump -Fc.
age_bin="${AGE_BIN:-$(command -v age || true)}"
if [ -z "$age_bin" ]; then
    echo "==> Проверка расшифровки пропущена: нет age (установить: brew install age)."
    exit 0
fi
if [ ! -f "$BACKUP_AGE_KEY" ]; then
    echo "==> Проверка расшифровки пропущена: нет приватного ключа $BACKUP_AGE_KEY"
    echo "    (как создать и куда положить — docs/runbooks/restore.md, «Шифрование»)."
    exit 0
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
