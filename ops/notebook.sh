#!/usr/bin/env bash
# Общие тетради (#532): «потом» и очередь основателя. Новая запись — отдельный файл
# в папке, поэтому PR параллельных окон не конфликтуют в конце общего файла.
# Печатает архив до 29.10.2026, затем все записи папки по дате (имя начинается с даты).
#   ops/notebook.sh later           — docs/LATER.md + docs/later/
#   ops/notebook.sh founder-review  — docs/FOUNDER_REVIEW_QUEUE.md + docs/founder-review/
#   ops/notebook.sh decisions [ГГГГ-ММ-ДД] — «покажи решения» (#909): только записи
#       docs/founder-review/ с непросмотренным «- [ ]», без архива; с датой — записи с этой даты
# Формат имени и записи — README.md папки.
set -euo pipefail
# Порядок записей = порядок имён; без C-локали сортировка глоба зависит от системы.
export LC_ALL=C
cd "$(dirname "$0")/.."

open_only=""
since=""
case "${1:-}" in
    later)
        archive="docs/LATER.md"
        folder="docs/later"
        ;;
    decisions)
        archive=""
        folder="docs/founder-review"
        open_only=1
        since="${2:-}"
        if [ -n "${since}" ] && ! [[ "${since}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
            echo "usage: ops/notebook.sh decisions [ГГГГ-ММ-ДД]" >&2
            exit 2
        fi
        ;;
    founder-review)
        archive="docs/FOUNDER_REVIEW_QUEUE.md"
        folder="docs/founder-review"
        ;;
    *)
        echo "usage: ops/notebook.sh later|founder-review|decisions [ГГГГ-ММ-ДД]" >&2
        exit 2
        ;;
esac

if [ -n "${archive}" ]; then
    echo "==> ${archive} (архив до 29.10.2026)"
    cat "${archive}"
fi

count=0
shopt -s nullglob
for entry in "${folder}"/*.md; do
    name="$(basename "${entry}")"
    if [ "${name}" = "README.md" ]; then
        continue
    fi
    # Запись с неверным именем всё равно печатается: молча пропущенная находка потеряна.
    if ! [[ "${name}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}-pr[0-9]+-[a-z0-9-]+\.md$ ]]; then
        echo "!! ${entry}: имя не по формату ГГГГ-ММ-ДД-pr<N>-<слово>.md (${folder}/README.md)" >&2
    fi
    if [ -n "${open_only}" ]; then
        grep -q -e '- \[ \]' "${entry}" || continue
        # Имя начинается с даты, поэтому сравнение строк — сравнение дат.
        if [ -n "${since}" ] && [[ "${name}" < "${since}" ]]; then
            continue
        fi
    fi
    echo
    echo "==> ${entry}"
    cat "${entry}"
    count=$((count + 1))
done

echo
if [ -n "${open_only}" ]; then
    echo "==> ${folder}/: непросмотренных записей ${count}${since:+ с ${since}}"
else
    echo "==> ${folder}/: записей ${count}"
fi
