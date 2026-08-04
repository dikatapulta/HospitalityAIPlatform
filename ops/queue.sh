#!/usr/bin/env bash
# Очередь работ: docs/QUEUE.md задаёт ПОРЯДОК, GitHub — живое СОСТОЯНИЕ.
# Скрипт сводит их вместе и показывает расхождения (правило исполнения 10).
# Запуск: make queue
set -euo pipefail

cd "$(dirname "$0")/.."
QUEUE_FILE="docs/QUEUE.md"

if ! command -v gh >/dev/null 2>&1; then
    echo "gh не установлен — показываю только файл, без сверки с GitHub." >&2
    sed -n '/^## 0\./,/^## 3\./p' "$QUEUE_FILE"
    exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

gh pr list --state open --limit 100 \
    --json number,title,reviewDecision,createdAt,body >"$tmp/prs.json"
gh issue list --state open --limit 200 \
    --json number,title,milestone >"$tmp/issues.json"

# Номера, упомянутые в QUEUE.md где угодно (включая раздел «отложено»).
grep -oE '#[0-9]+' "$QUEUE_FILE" | tr -d '#' | sort -un >"$tmp/mentioned.txt"

echo "==> Ждёт ревью и merge (раньше любого нового кода)"
if [ "$(jq 'length' "$tmp/prs.json")" -eq 0 ]; then
    echo "    пусто — можно брать новую задачу"
else
    jq -r '.[] | "\(.number)\t\(.reviewDecision // "REVIEW_REQUIRED")\t\(.createdAt[0:10])\t\(.title)"' \
        "$tmp/prs.json" | sort -n | while IFS=$'\t' read -r num decision created title; do
        case "$decision" in
        APPROVED) mark="approve есть — можно мержить" ;;
        CHANGES_REQUESTED) mark="changes requested — за автором" ;;
        *) mark="ревью не начато" ;;
        esac
        printf '    PR #%-5s %-28s с %s  %s\n' "$num" "$mark" "$created" "$title"
    done
fi

echo
echo "==> Следующее в работу — раздел 1 «Код», порядок из $QUEUE_FILE"
awk '/^## 1\. /,/^## 2\. /' "$QUEUE_FILE" |
    grep -oE '^[0-9]+\. \*\*#[0-9]+( \+ #[0-9]+)?\*\*' |
    sed -E 's/\*\*//g' >"$tmp/ordered.txt" || true

shown=0
while read -r line; do
    [ "$shown" -ge 5 ] && break
    pos="${line%%.*}"
    nums="$(echo "$line" | grep -oE '[0-9]+' | tail -n +2)"
    first="$(echo "$nums" | head -1)"
    title="$(jq -r --argjson n "$first" '.[] | select(.number == $n) | .title' "$tmp/issues.json")"
    if [ -z "$title" ]; then
        printf '    %2s. #%-5s [ЗАКРЫТ на GitHub — вычеркни строку]\n' "$pos" "$first"
        continue
    fi
    busy=""
    for n in $nums; do
        if jq -e --arg n "$n" \
            '.[] | select((.title + " " + (.body // "")) | test("#" + $n + "([^0-9]|$)"))' \
            "$tmp/prs.json" >/dev/null 2>&1; then
            busy=" ← уже есть открытый PR, задача занята"
        fi
    done
    printf '    %2s. #%-5s %s%s\n' "$pos" "$first" "${title:0:70}" "$busy"
    shown=$((shown + 1))
done <"$tmp/ordered.txt"
echo "    (полный список и модель/effort по каждому пункту — $QUEUE_FILE)"

echo
echo "==> Расхождения между $QUEUE_FILE и GitHub"
found=0

# Открытые issue майлстоуна, которых нет в очереди вообще.
jq -r '.[] | select(.milestone != null) | "\(.number)\t\(.title)"' "$tmp/issues.json" |
    while IFS=$'\t' read -r num title; do
        grep -qx "$num" "$tmp/mentioned.txt" || {
            printf '    ! issue #%-5s не вписан в очередь: %s\n' "$num" "${title:0:60}"
        }
    done >"$tmp/orphan_issues.txt"

# Открытые PR, которых нет в разделе 0.
jq -r '.[] | "\(.number)\t\(.title)"' "$tmp/prs.json" |
    while IFS=$'\t' read -r num title; do
        awk '/^## 0\./,/^## 1\./' "$QUEUE_FILE" | grep -q "/pull/$num" || {
            printf '    ! PR #%-5s не вписан в раздел 0: %s\n' "$num" "${title:0:60}"
        }
    done >"$tmp/orphan_prs.txt"

for f in "$tmp/orphan_prs.txt" "$tmp/orphan_issues.txt"; do
    if [ -s "$f" ]; then
        cat "$f"
        found=1
    fi
done
if [ "$found" -eq 0 ]; then
    echo "    нет — файл и GitHub сходятся"
else
    echo "    впиши недостающее в $QUEUE_FILE (правило исполнения 10) — молча пропускать нельзя"
fi
