#!/usr/bin/env bash
# Очередь работ (правило исполнения 10): docs/QUEUE.md задаёт ПОРЯДОК, GitHub — живое
# СОСТОЯНИЕ. Скрипт сводит их и показывает расхождения. Запуск: make queue.
#
# Контракт разметки docs/QUEUE.md, на который опирается разбор (описан и в самом файле):
#   - пункт раздела 1 — строка вида «- **#N …» (маркированный список, номер жирным);
#   - **#N** (жирным) в любом разделе — пункт очереди, обязан быть ОТКРЫТЫМ issue;
#   - #N обычным текстом — упоминание в обосновании, пунктом не считается.
# Раздел 0 (открытые PR) в файле не ведётся — печатается отсюда, живьём из GitHub.
# Для тестов (tests/test_queue_script.py) путь к файлу переопределяется: QUEUE_FILE=…
set -euo pipefail

cd "$(dirname "$0")/.."
QUEUE_FILE="${QUEUE_FILE:-docs/QUEUE.md}"

# GitHub недоступен — очередь всё равно нужна: показать файл и выйти без ошибки.
show_file_only() {
    echo "$1 — показываю только файл, без сверки с GitHub." >&2
    sed -n '/^## 0\./,$p' "$QUEUE_FILE"
    exit 0
}

command -v gh >/dev/null 2>&1 || show_file_only "gh не установлен"
command -v jq >/dev/null 2>&1 || show_file_only "jq не установлен"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

gh pr list --state open --limit 100 \
    --json number,title,isDraft,reviewDecision,createdAt,closingIssuesReferences \
    >"$tmp/prs.json" 2>/dev/null || show_file_only "GitHub недоступен (gh pr list)"
gh issue list --state open --limit 300 \
    --json number,title >"$tmp/issues.json" 2>/dev/null ||
    show_file_only "GitHub недоступен (gh issue list)"
jq -r '.[].number' "$tmp/issues.json" | sort -u >"$tmp/open_issues.txt"

echo "==> Раздел 0 — открытые PR (живьём из GitHub, старые сверху)"
if [ "$(jq 'length' "$tmp/prs.json")" -eq 0 ]; then
    echo "    пусто — можно брать раздел 1"
else
    jq -r 'sort_by(.createdAt)[]
        | "\(.number)\t\(if .isDraft then "DRAFT" else (.reviewDecision // "REVIEW_REQUIRED") end)\t\(.createdAt[0:10])\t\(.title)"' \
        "$tmp/prs.json" |
        while IFS=$'\t' read -r num state created title; do
            case "$state" in
            DRAFT) mark="черновик — взято другой сессией, не ревьюить" ;;
            APPROVED) mark="approve есть — ждёт merge основателя, очередь не держит" ;;
            CHANGES_REQUESTED) mark="changes requested — доработка за автором" ;;
            *) mark="ждёт ревью — раньше любого нового кода" ;;
            esac
            printf '    PR #%-4s %-48s с %s  %s\n' "$num" "$mark" "$created" "$title"
        done
fi

# «Занято» — номер issue в ЗАГОЛОВКЕ открытого PR (конвенция «… (#N)») или в его
# Closes-ссылках. Тело PR не смотрим: там номера упоминаются в прозе, и один PR
# ложно метил занятыми восемь пунктов очереди (ревью PR #176, находка Б1).
jq -r '.[] | ([.title | scan("#[0-9]+") | ltrimstr("#")]
        + [.closingIssuesReferences[]?.number | tostring]) | .[]' \
    "$tmp/prs.json" | sort -u >"$tmp/busy.txt"

echo
echo "==> Следующее в работу — раздел 1 «Код», порядок из $QUEUE_FILE"
awk '/^## 1\. /,/^## 2\. /' "$QUEUE_FILE" | grep -E '^- \*\*#[0-9]+' >"$tmp/ordered.txt" || true
if [ ! -s "$tmp/ordered.txt" ]; then
    echo "!! Не разобрал раздел 1 в $QUEUE_FILE (жду строки вида «- **#N …») — очередь не прочитана." >&2
    exit 1
fi

shown=0
pos=0
while IFS= read -r line; do
    [ "$shown" -ge 5 ] && break
    pos=$((pos + 1))
    nums="$(printf '%s\n' "$line" | grep -oE '^- \*\*#[0-9]+( \+ #[0-9]+)?\*\*' | grep -oE '[0-9]+' || true)"
    first="$(printf '%s\n' "$nums" | head -n 1)"
    [ -n "$first" ] || continue
    title="$(jq -r --argjson n "$first" 'map(select(.number == $n)) | first | .title // empty' \
        "$tmp/issues.json")"
    [ -n "$title" ] || continue # закрыт или не issue — скажет блок расхождений ниже
    busy=""
    for n in $nums; do
        grep -qx "$n" "$tmp/busy.txt" && busy=" ← занято: открытый PR с #$n в заголовке"
    done
    printf '    %2d. #%-4s %s%s\n' "$pos" "$first" "${title:0:70}" "$busy"
    shown=$((shown + 1))
done <"$tmp/ordered.txt"
echo "    (полный список, модель и effort по каждому пункту — $QUEUE_FILE)"

echo
echo "==> Расхождения между $QUEUE_FILE и GitHub"
# Пункт очереди (**#N** в любом разделе, включая 2 и 3) обязан быть открытым issue;
# каждый открытый issue обязан быть хотя бы упомянут в файле (#N в любом виде).
grep -oE '\*\*#[0-9]+( \+ #[0-9]+)?\*\*' "$QUEUE_FILE" | grep -oE '[0-9]+' | sort -u >"$tmp/claimed.txt"
grep -oE '#[0-9]+' "$QUEUE_FILE" | tr -d '#' | sort -u >"$tmp/mentioned.txt"

: >"$tmp/diff.txt"
while read -r n; do
    grep -qx "$n" "$tmp/open_issues.txt" ||
        echo "    ! пункт **#$n** закрыт на GitHub или не является issue — вычеркни строку" >>"$tmp/diff.txt"
done <"$tmp/claimed.txt"
jq -r '.[] | "\(.number)\t\(.title)"' "$tmp/issues.json" |
    while IFS=$'\t' read -r n title; do
        grep -qx "$n" "$tmp/mentioned.txt" ||
            echo "    ! issue #$n не вписан ни в один раздел: ${title:0:60}" >>"$tmp/diff.txt"
    done

if [ -s "$tmp/diff.txt" ]; then
    cat "$tmp/diff.txt"
    echo "    Поправь $QUEUE_FILE в своём PR (правило 10) — молча пропускать нельзя."
else
    echo "    нет — файл и GitHub сходятся"
fi
