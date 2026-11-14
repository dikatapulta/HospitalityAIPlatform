#!/usr/bin/env bash
# Общие тетради (#532): «потом» и очередь основателя. Новая запись — отдельный файл
# в папке, поэтому PR параллельных окон не конфликтуют в конце общего файла.
# Печатает архив до 29.10.2026, затем все записи папки по дате (имя начинается с даты).
#   ops/notebook.sh later           — docs/LATER.md + docs/later/
#   ops/notebook.sh founder-review  — docs/FOUNDER_REVIEW_QUEUE.md + docs/founder-review/
#   ops/notebook.sh decisions [--all] [ГГГГ-ММ-ДД] — «покажи решения» (#909): только записи
#       docs/founder-review/ с непросмотренным «- [ ]», без архива. По умолчанию — «красный»
#       список (#1058 п. 2): записи «Класс: красное», каждая — своей шапкой из трёх строк;
#       --all — все непросмотренные целиком. «Крупное» (#947) — первым; с датой — записи
#       с этой даты.
# Формат имени, записи и шапки — README.md папки.
set -euo pipefail
# Порядок записей = порядок имён; без C-локали сортировка глоба зависит от системы.
export LC_ALL=C
cd "$(dirname "$0")/.."

# С этой даты у записи обязана быть шапка (#1058 п. 2). Запись без неё не прячется:
# в «красном» списке она печатается целиком, с предупреждением. Старше — обычная,
# кроме «крупного» по прежней пометке «— крупное:».
header_since="2026-11-14"

usage() {
    echo "usage: ops/notebook.sh later|founder-review|decisions [--all] [ГГГГ-ММ-ДД]" >&2
    exit 2
}

decisions=""
show_all=""
since=""
case "${1:-}" in
    later)
        archive="docs/LATER.md"
        folder="docs/later"
        ;;
    decisions)
        archive=""
        folder="docs/founder-review"
        decisions=1
        shift
        for arg in "$@"; do
            if [ "${arg}" = "--all" ]; then
                show_all=1
            elif [[ "${arg}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
                since="${arg}"
            else
                usage
            fi
        done
        ;;
    founder-review)
        archive="docs/FOUNDER_REVIEW_QUEUE.md"
        folder="docs/founder-review"
        ;;
    *)
        usage
        ;;
esac

if [ -n "${archive}" ]; then
    echo "==> ${archive} (архив до 29.10.2026)"
    cat "${archive}"
fi

# Раскладка по группам; печать — группа за группой, внутри группы — по дате.
big=()
red=()
plain=()
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
    if [ -z "${decisions}" ]; then
        plain+=("${entry}")
        continue
    fi
    grep -q -e '- \[ \]' "${entry}" || continue
    # Имя начинается с даты, поэтому сравнение строк — сравнение дат.
    if [ -n "${since}" ] && [[ "${name}" < "${since}" ]]; then
        continue
    fi
    first="$(head -n 1 "${entry}")"
    case "${first}" in
        "Класс: красное"*)
            # Подклассы — до первого двоеточия после «Класс:», дальше — «почему» свободным текстом.
            kinds="${first#Класс: }"
            if [[ "${kinds%%:*}" == *крупное* ]]; then big+=("${entry}"); else red+=("${entry}"); fi
            ;;
        "Класс: обычное"*)
            plain+=("${entry}")
            ;;
        *)
            if grep -q -e '— крупное:' "${entry}"; then
                big+=("${entry}")
            elif [[ "${name}" < "${header_since}" ]]; then
                plain+=("${entry}")
            else
                red+=("${entry}")
            fi
            ;;
    esac
done

print_entry() {
    echo
    echo "==> $1"
    if [ -n "${decisions}" ] && [ -z "${show_all}" ]; then
        case "$(head -n 1 "$1")" in
            "Класс: "*)
                head -n 3 "$1"
                return
                ;;
        esac
        if ! [[ "$(basename "$1")" < "${header_since}" ]]; then
            echo "!! $1: нет шапки «Класс: …» (docs/founder-review/README.md) — запись целиком" >&2
        fi
    fi
    cat "$1"
}

# bash 3.2 (macOS) под set -u считает пустой массив несвязанным — отсюда «${a[@]+…}».
count=0
for entry in ${big[@]+"${big[@]}"} ${red[@]+"${red[@]}"}; do
    print_entry "${entry}"
    count=$((count + 1))
done
if [ -z "${decisions}" ] || [ -n "${show_all}" ]; then
    for entry in ${plain[@]+"${plain[@]}"}; do
        print_entry "${entry}"
        count=$((count + 1))
    done
fi

echo
if [ -z "${decisions}" ]; then
    echo "==> ${folder}/: записей ${count}"
elif [ -n "${show_all}" ]; then
    echo "==> ${folder}/: непросмотренных записей ${count}${since:+ с ${since}}"
else
    echo "==> ${folder}/: красных записей ${count}${since:+ с ${since}} из непросмотренных $((count + ${#plain[@]}))" \
        "— остальные: make decisions ALL=1"
fi
