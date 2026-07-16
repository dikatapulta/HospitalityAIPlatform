#!/usr/bin/env bash
# Offsite-копия бэкапа (Task 0019, §10.11 bus factor = 1): забрать последний
# дамп Postgres со staging на машину основателя. Пока сервер — единственная
# машина с данными, еженедельный запуск `make backup-fetch` — та самая копия,
# которая переживёт потерю сервера (docs/runbooks/restore.md).
set -euo pipefail

cd "$(dirname "$0")/../.."
# shellcheck source=ops/staging-ssh-lib.sh
source ops/staging-ssh-lib.sh

latest="$(staging_ssh "ls -1t /opt/hospitality/backups/hospitality-*.dump 2>/dev/null | head -1")"
if [ -z "$latest" ]; then
    echo "На сервере нет ни одного дампа в /opt/hospitality/backups —" >&2
    echo "бэкапы ещё не настроены? См. docs/runbooks/restore.md, раздел «Расписание»." >&2
    exit 1
fi

mkdir -p backups
staging_scp_from "$latest" backups/
echo "==> OK: backups/$(basename "$latest")"
