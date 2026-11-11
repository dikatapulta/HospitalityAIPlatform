#!/usr/bin/env bash
# Очередь работ (правило исполнения 10): docs/QUEUE.md задаёт ПОРЯДОК, GitHub — живое
# СОСТОЯНИЕ. Скрипт сводит их и показывает расхождения. Запуск: make queue.
#
# Контракт разметки docs/QUEUE.md, на который опирается разбор (описан и в самом файле):
#   - пункт раздела 1 — строка вида «- **#N …» (маркированный список, номер жирным);
#     группа родственных пунктов (правило 2) — «- **#N + #N + #N**», слагаемых сколько угодно;
#   - **#N** (жирным) в любом разделе — пункт очереди, обязан быть ОТКРЫТЫМ issue;
#   - #N обычным текстом — упоминание в обосновании, пунктом не считается;
#   - «[ждёт основателя]» в первой строке пункта — окна и сторож его пропускают (#743);
#   - модель пункта — «**Fable …**» в любой строке пункта (продолжения — с отступом), иначе Opus.
# Раздел 0 (открытые PR) в файле не ведётся — печатается отсюда, живьём из GitHub.
# `--free` — тот же разбор для сторожа пула (ops/pool-spawn.sh, #743): строки «pr N состояние»
# по каждому открытому PR и «item N opus|fable» по незанятым пунктам раздела 1, не ждущим
# основателя; GitHub не ответил или раздел 1 не разобран — ненулевой код.
# Статус PR — не голый `reviewDecision`: GitHub держит там CHANGES_REQUESTED до тех
# пор, пока не подан НОВЫЙ review, то есть и после того, как автор запушил доработку
# и попросил повторный проход. Различаем по коммиту: у каждого review есть `commit`,
# который он читал; не совпал с `headRefOid` — ревьюер этой головы не видел, и
# состояние не «доработка за автором», а «ждёт повторного прохода» (живой случай —
# PR #315, 25.08.2026: review прочитал 98c7a50, доработка ушла в 7e6049c, CI зелёный,
# а раздел 0 звал переделывать уже переделанное). Голову ищем среди review, НЕСУЩИХ
# вердикт (`CHANGES_REQUESTED`/`APPROVED`): `reviewDecision` ставят только они, а
# review другого состояния, окажись он самым свежим, сравнял бы `commit` с головой
# и вернул прежний ответ. Оба таких состояния у нас возможны: `COMMENTED` вердикта
# не несёт, а `DISMISSED` GitHub проставляет approve'у на новом коммите в ветке
# (`dismiss_stale_reviews`).
# Вердикт комментарием (#324): approve вторым аккаунтом безголовому окну отклоняет
# классификатор режима auto, и вердикт остаётся комментарием основного аккаунта — в
# `reviews` его нет, и раздел 0 звал ревьюировать уже отревьюированное (PR #960, #995:
# по 2–4 окна). Читаем его по первой строке «Вердикт комментарием: approve|changes,
# голова <sha 40>» — от владельца или участника репозитория (репозиторий публичный).
# Свежее последнего review с вердиктом — решает он: approve на текущей голове —
# APPROVE_COMMENTED (ревью не повторять, approve ставит окно с человеком), changes —
# доработка за автором, голова сдвинулась — ждёт повторного прохода.
# Эти состояния — факт правила 10 PROJECT_EXECUTION_PLAN: меняешь разбор тут — правишь
# и правило тем же PR.
# Для тестов (tests/test_queue_script.py) путь к файлу переопределяется: QUEUE_FILE=…
set -euo pipefail

cd "$(dirname "$0")/.."
QUEUE_FILE="${QUEUE_FILE:-docs/QUEUE.md}"
case "${1:-}" in
'') FREE=0 ;;
--free) FREE=1 ;;
*) echo "использование: ops/queue.sh [--free]" >&2; exit 2 ;;
esac

# GitHub недоступен — очередь всё равно нужна: показать файл и выйти без ошибки.
# Сторожу (--free) файл без живого состояния не ответ: код 2, он запустит окно как раньше.
show_file_only() {
    if [ "$FREE" = 1 ]; then echo "$1" >&2; exit 2; fi
    echo "$1 — показываю только файл, без сверки с GitHub." >&2
    sed -n '/^## 0\./,$p' "$QUEUE_FILE"
    exit 0
}

command -v gh >/dev/null 2>&1 || show_file_only "gh не установлен"
command -v jq >/dev/null 2>&1 || show_file_only "jq не установлен"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

gh pr list --state open --limit 100 \
    --json number,title,isDraft,reviewDecision,createdAt,closingIssuesReferences,headRefOid,reviews,comments \
    >"$tmp/prs.json" 2>/dev/null || show_file_only "GitHub недоступен (gh pr list)"
gh issue list --state open --limit 300 \
    --json number,title >"$tmp/issues.json" 2>/dev/null ||
    show_file_only "GitHub недоступен (gh issue list)"
jq -r '.[].number' "$tmp/issues.json" | sort -u >"$tmp/open_issues.txt"

jq -r 'sort_by(.createdAt)[]
    | .headRefOid as $head
    | ((.reviews // [])
        | map(select(.state == "CHANGES_REQUESTED" or .state == "APPROVED"))
        | max_by(.submittedAt)) as $review
    | ($review | .commit.oid? // null) as $seen
    | ((.comments // [])
        | map(select(.authorAssociation == "OWNER" or .authorAssociation == "MEMBER"
                     or .authorAssociation == "COLLABORATOR")
              | .createdAt as $at
              | (.body // "") | capture("\\AВердикт комментарием: (?<verdict>approve|changes), голова (?<oid>[0-9a-f]{40})")
              | . + {at: $at})
        | max_by(.at)) as $comment
    | (if .isDraft then "DRAFT"
       elif $comment != null and $comment.at > ($review.submittedAt? // "")
       then (if $comment.oid != $head then "REWORK_PUSHED"
             elif $comment.verdict == "approve" then "APPROVE_COMMENTED"
             else "CHANGES_REQUESTED" end)
       elif (.reviewDecision // "") == "CHANGES_REQUESTED"
            and $seen != null and $head != null and $seen != $head
       then "REWORK_PUSHED"
       else (.reviewDecision // "REVIEW_REQUIRED") end) as $state
    | "\(.number)\t\($state)\t\(.createdAt[0:10])\t\(.title)"' \
    "$tmp/prs.json" >"$tmp/pr_states.tsv"

if [ "$FREE" = 0 ]; then
    echo "==> Раздел 0 — открытые PR (живьём из GitHub, старые сверху)"
    if [ ! -s "$tmp/pr_states.tsv" ]; then echo "    пусто — можно брать раздел 1"; fi
    while IFS=$'\t' read -r num state created title; do
        case "$state" in
        DRAFT) mark="черновик — взято другой сессией, не ревьюить" ;;
        APPROVED) mark="approve есть — мержить, это первое действие сессии" ;;
        APPROVE_COMMENTED) mark="approve комментарием — ревью не повторять, approve ставит окно с человеком" ;;
        REWORK_PUSHED) mark="доработка запушена — ждёт повторного прохода" ;;
        CHANGES_REQUESTED) mark="changes requested — доработка за автором" ;;
        *) mark="ждёт ревью — раньше любого нового кода" ;;
        esac
        printf '    PR #%-4s %-48s с %s  %s\n' "$num" "$mark" "$created" "$title"
    done <"$tmp/pr_states.tsv"
fi

# «Занято» — номер issue в ЗАГОЛОВКЕ открытого PR (конвенция «… (#N)») или в его
# Closes-ссылках. Тело PR не смотрим: там номера упоминаются в прозе, и один PR
# ложно метил занятыми восемь пунктов очереди (ревью PR #176, находка Б1).
# Диапазон «#A–#B» (тире или дефис) в заголовке — разноска пакета, а не заявка: его
# номера не берём, иначе концы диапазона ложно «заняты» (#240, PR #238 «(#206–#237)»).
jq -r '.[] | ([.title | gsub("#[0-9]+\\s*[-–—]\\s*#?[0-9]+"; "") | scan("#[0-9]+") | ltrimstr("#")]
        + [.closingIssuesReferences[]?.number | tostring]) | .[]' \
    "$tmp/prs.json" | sort -u >"$tmp/busy.txt"

# Пункт раздела 1 — строка «- **#N …» и её продолжения с отступом; на пункт — строка
# «модель<TAB>ждёт основателя 0|1<TAB>первая строка».
awk '/^## 1\. /,/^## 2\. /' "$QUEUE_FILE" | awk '
    function flush() { if (first != "") printf "%s\t%d\t%s\n", model, held, first; first = "" }
    /^- \*\*#[0-9]+/ {
        flush(); first = $0; model = "opus"; held = index($0, "[ждёт основателя]") > 0
        if ($0 ~ /\*\*Fable [0-9]/) model = "fable"
        next
    }
    first != "" && /^[ \t]/ { if ($0 ~ /\*\*Fable [0-9]/) model = "fable"; next }
    { flush() }
    END { flush() }' >"$tmp/ordered.tsv"
if [ ! -s "$tmp/ordered.tsv" ]; then
    echo "!! Не разобрал раздел 1 в $QUEUE_FILE (жду строки вида «- **#N …») — очередь не прочитана." >&2
    exit 1
fi

if [ "$FREE" = 1 ]; then
    cut -f1,2 "$tmp/pr_states.tsv" | while IFS=$'\t' read -r num state; do echo "pr $num $state"; done
else
    echo
    echo "==> Следующее в работу — раздел 1 «Код», порядок из $QUEUE_FILE"
fi
shown=0
pos=0
while IFS=$'\t' read -r model held line; do
    [ "$FREE" = 0 ] && [ "$shown" -ge 5 ] && break
    pos=$((pos + 1))
    nums="$(printf '%s\n' "$line" | grep -oE '^- \*\*#[0-9]+( \+ #[0-9]+)*\*\*' | grep -oE '[0-9]+' || true)"
    # Первый номер — без трубы: `| head -n 1` закрывает её раньше printf, и под
    # pipefail присваивание изредка падало кодом 141 (SIGPIPE) вместе со скриптом (#582).
    first="${nums%%$'\n'*}"
    [ -n "$first" ] || continue
    title="$(jq -r --argjson n "$first" 'map(select(.number == $n)) | first | .title // empty' \
        "$tmp/issues.json")"
    [ -n "$title" ] || continue # закрыт или не issue — скажет блок расхождений ниже
    busy=""
    for n in $nums; do
        grep -qx "$n" "$tmp/busy.txt" && busy=" ← занято: открытый PR с #$n в заголовке"
    done
    if [ -z "$busy" ] && [ "$held" = 1 ]; then busy=" ← ждёт основателя — окна пропускают"; fi
    if [ "$FREE" = 1 ]; then
        if [ -z "$busy" ]; then echo "item $first $model"; fi
        continue
    fi
    printf '    %2d. #%-4s %s%s\n' "$pos" "$first" "${title:0:70}" "$busy"
    shown=$((shown + 1))
done <"$tmp/ordered.tsv"
if [ "$FREE" = 1 ]; then exit 0; fi
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
        echo "    ! пункт **#$n** закрыт на GitHub или не является issue — строку вычеркнет диспетчер" >>"$tmp/diff.txt"
done <"$tmp/claimed.txt"
jq -r '.[] | "\(.number)\t\(.title)"' "$tmp/issues.json" |
    while IFS=$'\t' read -r n title; do
        grep -qx "$n" "$tmp/mentioned.txt" ||
            echo "    ! issue #$n не вписан ни в один раздел: ${title:0:60}" >>"$tmp/diff.txt"
    done

if [ -s "$tmp/diff.txt" ]; then
    cat "$tmp/diff.txt"
    # С 29.10.2026 (#544) строки очереди правит только PR очереди окна-диспетчера (правило 10):
    # прежнее «поправь в своём PR» сводило параллельные PR в конфликт в одном файле.
    echo "    Закроет их PR очереди окна-диспетчера (правило 10); рабочая сессия идёт дальше,"
    echo "    строки $QUEUE_FILE свой PR не правит."
else
    echo "    нет — файл и GitHub сходятся"
fi
