# shellcheck shell=bash
# Окно → очередь слияния: вердикт ревью аккаунтом ревьюера и постановка в очередь.
# Подключается из ops/pool.sh после ops/pool-merge.sh — очередь, воркер и review аккаунтом
# ревьюера (reviewer_review) там. Шаги окна — docs/QUEUE.md, «Пул окон»; механика ревью —
# docs/runbooks/review-process.md. Тесты — tests/test_pool_verdict.py.
#
# Вердикт — только скриптом (#1058 п. 1, слово основателя 14.11; закрывает #873): прежде approve
# второго аккаунта ничем, кроме вероятностного классификатора, не был привязан к независимому
# ревью. Approve ставит только окно с заявкой review-<PR> и только на голову, которую оно само
# держало (она в журнале HEAD его копии, `git log -g`) и в которую влит свежий main. Окно,
# в чьей копии создан хоть один commit PR, кроме записей «потом» и номера миграции, которые
# ревьюер пушит сам (правило 9), — автор: свой PR оно не одобряет. Безголовым окнам
# классификатор пропускает только этот вызов, прямой — нет (ops/pool-worker-settings.json).
# Посторонний PR (ops/pool-trust.sh, #1059) не получает ни вердикта, ни места в очереди.

refuse() { echo "pool: $*" >&2; exit 1; }

pr_number() { case "$1" in '' | *[!0-9]*) die "PR — номер, а не «${1}»" ;; esac; }

require_trusted_pr() { # PR — свой, иначе отказ кодом 1
    local out login cross
    out="$(gh pr view "$1" --json author,isCrossRepository --jq '"\(.author.login) \(.isCrossRepository)"')" ||
        die "GitHub не ответил о PR #${1}"
    read -r login cross <<<"$out"
    if [ "$cross" = false ] && trusted_author "$login"; then return 0; fi
    refuse "PR #${1} посторонний (автор ${login:-?}, из форка: ${cross:-?}) — пул его не ведёт: не ревьюить, не запускать его код, сказать основателю"
}

# Одобренная — commit, на который поставлен живой approve (не голова: approve мог
# ещё не сняться после push). Нет живого approve — отказ, код 1.
approved_of() {
    local out state draft decision verdict approved
    require_trusted_pr "$1"
    out="$(pr_state "$1")" || die "GitHub не ответил о PR #${1}"
    read -r state draft decision verdict approved _ <<<"$out"
    if [ "${state} ${draft} ${decision} ${verdict}" != "OPEN false APPROVED APPROVED" ]; then
        refuse "PR #${1} — ${state}, черновик: ${draft}, решение: ${decision}, последний вердикт: ${verdict}; нужен открытый PR с живым approve"
    fi
    echo "$approved"
}

cmd_enqueue() {
    local front="" stamp place="" approved tmp
    if [ "${1:-}" = --front ]; then front=1 && shift; fi
    [ $# -eq 1 ] || die "использование: ops/pool.sh enqueue [--front] <PR>"
    pr_number "$1"
    need_owner
    if ! worker_alive; then
        refuse "воркера слияния нет — мержи сам: ops/pool.sh merge-one ${1} (в фоне)"
    fi
    approved="$(approved_of "$1")" || exit $?
    if [ -n "$front" ]; then
        stamp="0$(date '+%Y%m%d%H%M%S')" place=" первым — срочно"
    elif stamp="$(cat "$DROPPED/$1" 2>/dev/null)" && [ "${stamp%-*}" != "$stamp" ]; then
        stamp="${stamp%-*}" place=" на прежнее место"
    else
        stamp="$(date '+%Y%m%d%H%M%S')"
    fi
    mkdir -p "$MERGE_QUEUE"
    rm -f "$MERGE_QUEUE"/*-"$1" "$DROPPED/$1"
    tmp="$MERGE_QUEUE/.new.$$"
    printf '%s %s\n' "$1" "$approved" >"$tmp"
    mv "$tmp" "$MERGE_QUEUE/${stamp}-$1"
    echo "pool: PR #${1} в очереди слияния${place} (одобрен ${approved:0:8}) — воркер обновит его до main, дождётся check и смержит"
}

# Первый commit PR, созданный в этой копии (журнал HEAD: commit, cherry-pick, rebase, revert —
# не checkout и не fetch), кроме своих commit'ов ревьюера: только docs/later/, миграция
# и head в tests/test_db.py. Слияния с main сюда не входят. 1 — таких нет.
authored_here() { # голова
    local made sha files f own
    made=" $(git log -g --format='%H %gs' HEAD 2>/dev/null |
        awk '$2 ~ /^(commit|cherry-pick|rebase|revert)/ { printf "%s ", $1 }')"
    for sha in $(git rev-list --no-merges "refs/remotes/origin/main..$1"); do
        case "$made" in *" $sha "*) ;; *) continue ;; esac
        files="$(git diff-tree --no-commit-id --name-only -r "$sha")"
        own=0
        for f in $files; do
            case "$f" in docs/later/* | alembic/versions/* | tests/test_db.py) own=1 ;; *) own=0 && break ;; esac
        done
        if [ "$own" = 0 ]; then echo "$sha"; return 0; fi
    done
    return 1
}

approve_checks() { # PR голова — approve только на голову, сведённую и прочитанную этим окном
    local seen sha
    fetch_pr "$1" "$2" || die "git не получил main и голову PR #${1}"
    git merge-base --is-ancestor refs/remotes/origin/main "$2" ||
        refuse "в голове ${2:0:8} нет свежего main — первый шаг ревью ещё раз (docs/runbooks/review-process.md), затем снова"
    seen=" $(git log -g --format=%H HEAD 2>/dev/null | tr '\n' ' ')"
    case "$seen" in *" $2 "*) ;; *) refuse "голова ${2:0:8} не из этой копии: approve ставит окно, которое само свело и прочло эту голову" ;; esac
    if sha="$(authored_here "$2")"; then
        refuse "commit ${sha:0:8} этого PR создан в твоей копии — автор свой PR не одобряет (FOUNDATION §13): ревью — другое окно"
    fi
}

cmd_verdict() { # APPROVE|REQUEST_CHANGES PR голова файл
    local event="$1" word="approve" out state draft head body
    shift
    if [ "$event" != APPROVE ]; then word="request-changes"; fi
    [ $# -eq 3 ] || die "использование: ops/pool.sh ${word} <PR> <голова — sha из 40 знаков> <файл вердикта>"
    pr_number "$1"
    case "$2" in *[!0-9a-f]*) die "голова — sha из 40 знаков, а не «${2}»" ;; esac
    [ "${#2}" = 40 ] || die "голова — sha из 40 знаков, а не «${2}»"
    [ -s "$3" ] || die "файла вердикта нет или он пуст: ${3}"
    need_owner
    my_slot >/dev/null || die "сначала номер окна: ops/pool.sh slot"
    is_mine "$POOL_DIR/review-$1" || refuse "вердикт по PR #${1} — только окну с заявкой review-${1} (ops/pool.sh claim в турникете)"
    require_trusted_pr "$1"
    out="$(pr_state "$1")" || die "GitHub не ответил о PR #${1}"
    read -r state draft _ _ _ head <<<"$out"
    [ "${state} ${draft}" = "OPEN false" ] || refuse "PR #${1} — ${state}, черновик: ${draft}; вердикт — только открытому PR не в черновике"
    [ "$head" = "$2" ] || refuse "голова PR #${1} — ${head:0:8}, а не ${2:0:8}: после твоего ревью в ветку пушили или ты не запушил — сверь и прочитай дельту"
    if [ "$event" = APPROVE ]; then approve_checks "$1" "$2"; fi
    REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" || die "gh не ответил"
    body="$(cat "$3")

—
${word} — скриптом пула (ops/pool.sh ${word}): окно $(my_slot), заявка review-${1}, голова ${2:0:8}."
    reviewer_review "$1" "$2" "$event" "$body" ||
        refuse "${word} аккаунтом ревьюера не прошёл (${REVIEWER_GH_CONFIG}; токен?) — вердикт комментарием, docs/runbooks/review-process.md"
    if [ "$event" = APPROVE ]; then
        echo "pool: approve на ${2:0:8} — дальше ops/pool.sh enqueue ${1}"
    else
        echo "pool: request changes на ${2:0:8} — доработка за автором"
    fi
}
