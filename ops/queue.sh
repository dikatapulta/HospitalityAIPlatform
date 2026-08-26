#!/usr/bin/env bash
# Очередь работ (правило исполнения 10): docs/QUEUE.md задаёт ПОРЯДОК, GitHub — живое
# СОСТОЯНИЕ. Скрипт сводит их и показывает расхождения. Запуск: make queue.
#
# Контракт разметки docs/QUEUE.md, на который опирается разбор (описан и в самом файле):
#   - пункт раздела 1 — строка вида «- **#N …» (маркированный список, номер жирным);
#     группа родственных пунктов (правило 2) — «- **#N + #N + #N**», слагаемых сколько угодно;
#   - **#N** (жирным) в любом разделе — пункт очереди, обязан быть ОТКРЫТЫМ issue;
#   - #N обычным текстом — упоминание в обосновании, пунктом не считается.
# Раздел 0 (открытые PR) в файле не ведётся — печатается отсюда, живьём из GitHub.
# Статус PR — не голый `reviewDecision`: GitHub держит там CHANGES_REQUESTED до тех
# пор, пока не подан НОВЫЙ review, то есть и после того, как автор запушил доработку
# и попросил повторный проход. Различаем по коммиту: у каждого review есть `commit`,
# который он читал; не совпал с `headRefOid` — ревьюер этой головы не видел, и
# состояние не «доработка за автором», а «ждёт повторного прохода» (живой случай —
# PR #315, 25.08.2026: review прочитал 98c7a50, доработка ушла в 7e6049c, CI зелёный,
# а раздел 0 звал переделывать уже переделанное). Голову ищем среди review, НЕСУЩИХ
# вердикт (`CHANGES_REQUESTED`/`APPROVED`): `reviewDecision` ставят только они, а
# проход ревью у нас приходил и целиком в `COMMENTED` (PR #295) — самый свежий
# review любого состояния сравнял бы `commit` с головой и вернул прежний ответ.
# Само это состояние — факт правила 10 PROJECT_EXECUTION_PLAN (там же и четыре
# остальных): меняешь разбор тут — правишь и правило тем же PR.
# Для тестов (tests/test_queue_script.py) путь к файлу переопределяется: QUEUE_FILE=…
set -euo pipefail

cd "$(dirname "$0")/.."
QUEUE_FILE="${QUEUE_FILE:-docs/QUEUE.md}"

# Процесс-батч (правило 11): копилка правок процесса и её пороги созревания.
# Числа — факт правила 11 PROJECT_EXECUTION_PLAN, здесь их исполняемая копия:
# меняешь порог тут — правишь и правило тем же PR (CLAUDE.md, владельцы фактов).
BATCH_ISSUE="${BATCH_ISSUE:-275}"
BATCH_LINES=10
BATCH_AGE_DAYS=7

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
    --json number,title,isDraft,reviewDecision,createdAt,closingIssuesReferences,headRefOid,reviews \
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
        | .headRefOid as $head
        | (((.reviews // [])
            | map(select(.state == "CHANGES_REQUESTED" or .state == "APPROVED"))
            | max_by(.submittedAt) | .commit.oid?) // null) as $seen
        | (if .isDraft then "DRAFT"
           elif (.reviewDecision // "") == "CHANGES_REQUESTED"
                and $seen != null and $head != null and $seen != $head
           then "REWORK_PUSHED"
           else (.reviewDecision // "REVIEW_REQUIRED") end) as $state
        | "\(.number)\t\($state)\t\(.createdAt[0:10])\t\(.title)"' \
        "$tmp/prs.json" |
        while IFS=$'\t' read -r num state created title; do
            case "$state" in
            DRAFT) mark="черновик — взято другой сессией, не ревьюить" ;;
            APPROVED) mark="approve есть — мержить, это первое действие сессии" ;;
            REWORK_PUSHED) mark="доработка запушена — ждёт повторного прохода" ;;
            CHANGES_REQUESTED) mark="changes requested — доработка за автором" ;;
            *) mark="ждёт ревью — раньше любого нового кода" ;;
            esac
            printf '    PR #%-4s %-48s с %s  %s\n' "$num" "$mark" "$created" "$title"
        done
fi

# Созревание процесс-батча считает машина, а не сессия по календарю: «раз в неделю»
# без подписчика — обязанность без исполнителя (правило 11). Строка копилки — «- [ ]»
# в теле issue или в его комментарии; возраст берётся от даты комментария.
echo
echo "==> Процесс-батч #$BATCH_ISSUE — копилка правок процесса (правило 11)"
if gh issue view "$BATCH_ISSUE" --json body,comments,createdAt >"$tmp/batch.json" 2>/dev/null &&
    batch="$(jq -r '
        ([{createdAt: .createdAt, body: .body}] + [.comments[] | {createdAt: .createdAt, body: .body}])
        | map(select(.body != null))
        | map({createdAt: .createdAt,
               open: (.body | split("\n") | map(select(test("^\\s*- \\[ \\]"))) | length)})
        | map(select(.open > 0))
        | ((map(.open) | add) // 0) as $lines
        | ((map(.createdAt | fromdateiso8601) | min) // null) as $oldest
        | "\($lines)\t\(if $oldest == null then -1 else ((now - $oldest) / 86400 | floor) end)"
    ' "$tmp/batch.json" 2>/dev/null)"; then
    lines="${batch%%$'\t'*}"
    age="${batch##*$'\t'}"
    if [ "$lines" -ge "$BATCH_LINES" ] || [ "$age" -ge "$BATCH_AGE_DAYS" ]; then
        printf '    СОЗРЕЛ: строк %s (порог %s), старейшей %s дн. (порог %s).\n' \
            "$lines" "$BATCH_LINES" "$age" "$BATCH_AGE_DAYS"
        echo "    Вносится одним PR ВЗАМЕН нового пункта раздела 1, а не в дополнение (правила 10 и 11)."
    elif [ "$age" -lt 0 ]; then
        echo "    пусто — копить нечего"
    else
        printf '    не созрел: строк %s из %s, старейшей %s дн. из %s — копим дальше\n' \
            "$lines" "$BATCH_LINES" "$age" "$BATCH_AGE_DAYS"
    fi
else
    # «Закрыт» тут не причина: gh issue view читает закрытый issue штатно и возвращает 0.
    # Закрытие #275 ловит блок «Расхождения» — пункт **#275** обязан быть открытым.
    echo "    не прочитан (нет доступа, issue удалён или GitHub не ответил) — порог проверь глазами, правило 11" >&2
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
    nums="$(printf '%s\n' "$line" | grep -oE '^- \*\*#[0-9]+( \+ #[0-9]+)*\*\*' | grep -oE '[0-9]+' || true)"
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
grep -oE '\*\*#[0-9]+( \+ #[0-9]+)*\*\*' "$QUEUE_FILE" | grep -oE '[0-9]+' | sort -u >"$tmp/claimed.txt"
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
