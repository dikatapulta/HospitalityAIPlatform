# shellcheck shell=bash
# Конвейер слияния пула окон (решение основателя 28.10): очередь, воркер без модели,
# запасной замок. Подключается из ops/pool.sh — замки и владелец там. Когда воркер
# вправе повторить approve — правило 9 PROJECT_EXECUTION_PLAN («Merge»); шаги окон —
# docs/QUEUE.md, «Пул окон». Тесты — tests/test_pool_merge.py.
#
# Запись очереди — файл merge-queue/<ГГГГММДДччммсс>-<PR> со строкой «PR голова patch-id»:
# голова, которую одобрил ревьюер, и patch-id её трёхточечного diff от origin/main —
# собственного diff PR. Влитый в ветку main этот diff не меняет, правка содержимого —
# меняет: совпал patch-id и check зелёный — содержимое то, что одобрил ревьюер.

MERGE_QUEUE="$POOL_DIR/merge-queue"
MERGING_MAX_MIN="${POOL_MERGING_MAX_MIN:-45}"
MERGE_ENTER_TIMEOUT_SEC="${POOL_MERGE_ENTER_TIMEOUT_SEC:-7200}"
MERGE_ENTER_INTERVAL_SEC="${POOL_MERGE_ENTER_INTERVAL_SEC:-10}"
WORKER_INTERVAL_SEC="${POOL_WORKER_INTERVAL_SEC:-30}"
WORKER_IDLE_MAX_SEC="${POOL_WORKER_IDLE_MAX_SEC:-}" # пусто — воркер ждёт очередь бессрочно
CHECK_INTERVAL_SEC="${POOL_CHECK_INTERVAL_SEC:-30}"
CHECK_MAX_SEC="${POOL_CHECK_MAX_SEC:-2400}"
MERGE_ATTEMPTS="${POOL_MERGE_ATTEMPTS:-5}"
REVIEWER_GH_CONFIG="${POOL_REVIEWER_GH_CONFIG:-$HOME/.config/gh-reviewer}"
REPO=""
HOLD_MERGING=0

worker_alive() { [ -d "$POOL_DIR/merge-worker" ] && ! lock_stale "$POOL_DIR/merge-worker"; }

worker_is_me() {
    is_mine "$POOL_DIR/merge-worker" && [ "$(cat "$POOL_DIR/merge-worker/pid" 2>/dev/null)" = "$$" ] &&
        owner_alive "$OWNER"
}

first_entry() {
    local f
    for f in "$MERGE_QUEUE"/*-*; do
        if [ -f "$f" ]; then echo "$f"; return 0; fi
    done
}

log() { echo "$(date '+%H:%M') PR #${1}: ${2}"; }

# Пауза в циклах ожидания; счёт времени — не меньше секунды за шаг, иначе нулевой
# интервал (тесты) крутил бы цикл бесконечно.
step_of() { if [ "$1" -gt 0 ]; then echo "$1"; else echo 1; fi; }

# --- git: собственный diff PR ---------------------------------------------------

fetch_pr() { # свежий main и голова PR $1 в локальном git; $2 — ожидаемая голова
    git fetch -q origin main "refs/pull/$1/head" 2>/dev/null && git cat-file -e "$2^{commit}" 2>/dev/null
}

pr_patch_id() {
    git diff --full-index --no-color --no-ext-diff --no-textconv --no-renames \
        "refs/remotes/origin/main...$1" -- | git patch-id --stable | cut -d' ' -f1
}

# --- GitHub ----------------------------------------------------------------------

pr_state() { gh pr view "$1" --json state,headRefOid --jq '"\(.state) \(.headRefOid)"'; }

# «голова статус»: статус required check `check` на голове PR — pending, success или
# заключение GitHub в нижнем регистре (failure, cancelled, timed_out…).
pr_check() {
    gh pr view "$1" --json headRefOid,statusCheckRollup --jq '"\(.headRefOid) " + (
        [.statusCheckRollup[] | select(.name == "check")] | last
        | if . == null or .status != "COMPLETED" then "pending" else (.conclusion | ascii_downcase) end)'
}

pr_comment() { gh pr comment "$1" --body "$2" >/dev/null 2>&1 || log "$1" "комментарий в PR не ушёл"; }

# Review аккаунтом ревьюера на конкретную голову: сдвинулась голова — review относится
# к старой и merge не откроет.
reviewer_review() { # PR голова APPROVE|REQUEST_CHANGES текст
    GH_CONFIG_DIR="$REVIEWER_GH_CONFIG" gh api -X POST "repos/${REPO}/pulls/$1/reviews" \
        -f event="$3" -f commit_id="$2" -f body="$4" >/dev/null
}

wait_check() { # PR голова → success | moved | timeout | заключение check
    local waited=0 out step
    step="$(step_of "$CHECK_INTERVAL_SEC")"
    while :; do
        if out="$(pr_check "$1" 2>/dev/null)"; then
            if [ "${out%% *}" != "$2" ]; then echo moved; return 0; fi
            if [ "${out#* }" != pending ]; then echo "${out#* }"; return 0; fi
        fi
        if [ "$waited" -ge "$CHECK_MAX_SEC" ]; then echo timeout; return 0; fi
        sleep "$CHECK_INTERVAL_SEC"
        waited=$((waited + step))
    done
}

wait_moved() { # update-branch GitHub выполняет не сразу: ждать новой головы PR $1
    local waited=0 out step
    step="$(step_of "$CHECK_INTERVAL_SEC")"
    while [ "$waited" -lt 120 ]; do
        if out="$(pr_state "$1" 2>/dev/null)" && [ "${out#* }" != "$2" ]; then return 0; fi
        sleep "$CHECK_INTERVAL_SEC"
        waited=$((waited + step))
    done
}

approve_unchanged() { # PR голова одобренная — при отказе воркер останавливается (код 5)
    local text try
    text="Обновление до main, содержимое не менялось: patch-id собственного diff PR совпал с одобренной головой ${3:0:8}, required check \`check\` зелёный. Повторный approve — очередь слияния (ops/pool.sh merge-worker), правило 9."
    for try in 1 2; do
        if reviewer_review "$1" "$2" APPROVE "$text"; then return 0; fi
        [ "$try" = 2 ] || sleep "$WORKER_INTERVAL_SEC"
    done
    log "$1" "повторный approve аккаунтом ревьюера не прошёл (${REVIEWER_GH_CONFIG}; механика — docs/runbooks/review-process.md) — воркер остановлен, PR остаётся в очереди"
    exit 5
}

# --- один PR ---------------------------------------------------------------------

drop() { # запись снята; PR $2, причина $3
    rm -f "$1"
    log "$2" "снят из очереди — $3"
}

# 0 — запись разобрана (смержен или снят), 1 — сбой сети: повторить позже.
merge_entry() {
    local file="$1" pr approved patch out state head cur st err attempt=0
    read -r pr approved patch <"$file" || { drop "$file" "?" "запись не читается"; return 0; }
    while :; do
        attempt=$((attempt + 1))
        if [ "$attempt" -gt "$MERGE_ATTEMPTS" ]; then
            pr_comment "$pr" "Очередь слияния: PR снят — за ${MERGE_ATTEMPTS} попыток не смержился (main уходит вперёд или GitHub отказывает). Нужен merge руками по правилу 9."
            drop "$file" "$pr" "попытки кончились"
            return 0
        fi
        out="$(pr_state "$pr")" || return 1
        state="${out%% *}" head="${out#* }"
        if [ "$state" != OPEN ]; then drop "$file" "$pr" "PR уже ${state}"; return 0; fi
        fetch_pr "$pr" "$head" || return 1
        cur="$(pr_patch_id "$head")" || return 1
        if [ "$cur" != "$patch" ]; then
            pr_comment "$pr" "Очередь слияния: PR снят — нужен повторный проход. После approve голова сменилась не только слиянием с main: собственный diff PR (patch-id трёхточечного diff) уже не тот, что одобрен на ${approved:0:8}. После нового approve — снова ops/pool.sh enqueue ${pr}."
            drop "$file" "$pr" "собственный diff изменился"
            return 0
        fi
        if ! git merge-base --is-ancestor refs/remotes/origin/main "$head"; then
            if ! git merge-tree --write-tree refs/remotes/origin/main "$head" >/dev/null 2>&1; then
                st="Очередь слияния: PR снят — нужна доработка: конфликт с main. Влей main в ветку, разреши конфликт, затем повторный проход ревью."
                reviewer_review "$pr" "$head" REQUEST_CHANGES "$st" 2>/dev/null || pr_comment "$pr" "$st"
                drop "$file" "$pr" "конфликт с main"
                return 0
            fi
            if ! err="$(gh pr update-branch "$pr" 2>&1)"; then
                log "$pr" "update-branch не прошёл: ${err}"
                sleep "$WORKER_INTERVAL_SEC"
                continue
            fi
            log "$pr" "отставал от main — обновлён"
            wait_moved "$pr" "$head"
            continue
        fi
        st="$(wait_check "$pr" "$head")"
        case "$st" in
        success) ;;
        moved) continue ;;
        timeout)
            pr_comment "$pr" "Очередь слияния: PR снят — required check \`check\` не закончился за $((CHECK_MAX_SEC / 60)) мин на ${head:0:8}. Посмотри Actions; зелёный — approve и merge по правилу 9."
            drop "$file" "$pr" "check не дождался"
            return 0
            ;;
        *)
            pr_comment "$pr" "Очередь слияния: PR снят — нужна доработка: required check \`check\` красный (${st}) на ${head:0:8}, голове после обновления до main."
            drop "$file" "$pr" "check: ${st}"
            return 0
            ;;
        esac
        # Голова не та, что одобрена (влит main), — approve снят GitHub'ом, его повторяет воркер.
        if [ "$head" != "$approved" ]; then approve_unchanged "$pr" "$head" "$approved"; fi
        if err="$(gh pr merge "$pr" -R "$REPO" --squash --delete-branch --match-head-commit "$head" 2>&1)"; then
            drop "$file" "$pr" "смержен"
            return 0
        fi
        log "$pr" "merge не прошёл: ${err}"
        sleep "$WORKER_INTERVAL_SEC"
    done
}

# --- команды ---------------------------------------------------------------------

cmd_enqueue() {
    [ $# -eq 1 ] || die "использование: ops/pool.sh enqueue <PR>"
    case "$1" in '' | *[!0-9]*) die "PR — номер, а не «${1}»" ;; esac
    need_owner
    if ! worker_alive; then
        echo "pool: воркера слияния нет — мержи сам, по одному: ops/pool.sh merge-enter (в фоне), merge по правилу 9, ops/pool.sh merge-leave" >&2
        exit 1
    fi
    local out state draft decision head patch tmp
    out="$(gh pr view "$1" --json state,isDraft,reviewDecision,headRefOid \
        --jq '"\(.state) \(.isDraft) \(.reviewDecision) \(.headRefOid)"')" || die "GitHub не ответил о PR #${1}"
    read -r state draft decision head <<<"$out"
    if [ "${state} ${draft} ${decision}" != "OPEN false APPROVED" ]; then
        echo "pool: PR #${1} — ${state}, черновик: ${draft}, решение: ${decision}; в очередь — только открытый PR с живым approve" >&2
        exit 1
    fi
    fetch_pr "$1" "$head" || die "не получил из origin main и голову PR #${1}"
    patch="$(pr_patch_id "$head")" || die "не посчитал patch-id PR #${1}"
    mkdir -p "$MERGE_QUEUE"
    rm -f "$MERGE_QUEUE"/*-"$1"
    tmp="$MERGE_QUEUE/.new.$$"
    printf '%s %s %s\n' "$1" "$head" "$patch" >"$tmp"
    mv "$tmp" "$MERGE_QUEUE/$(date '+%Y%m%d%H%M%S')-$1"
    echo "pool: PR #${1} в очереди слияния (голова ${head:0:8}) — воркер обновит его до main, дождётся check и смержит"
}

take_merging() { # воркер ждёт замок слияния; 1 — воркер больше не он
    until acquire merging; do
        worker_is_me || return 1
        sleep "$(step_of "$MERGE_ENTER_INTERVAL_SEC")"
    done
    HOLD_MERGING=1
}

leave_merging() {
    if [ "$HOLD_MERGING" = 1 ] && is_mine "$POOL_DIR/merging"; then rm -rf "$POOL_DIR/merging"; fi
    HOLD_MERGING=0
}

worker_cleanup() {
    leave_merging
    if worker_is_me; then rm -rf "$POOL_DIR/merge-worker"; fi
}

cmd_merge_worker() {
    need_owner
    is_mine "$POOL_DIR/dispatcher" || die "воркер запускает окно-диспетчер: сначала ops/pool.sh dispatcher"
    if ! acquire merge-worker; then
        echo "pool: воркер слияния уже работает" >&2
        exit 1
    fi
    printf '%s\n' "$$" >"$POOL_DIR/merge-worker/.pid.$$"
    mv "$POOL_DIR/merge-worker/.pid.$$" "$POOL_DIR/merge-worker/pid"
    trap worker_cleanup EXIT
    REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" || die "gh не ответил — воркер не запущен"
    mkdir -p "$MERGE_QUEUE"
    echo "pool: воркер слияния работает (${REPO}), очередь — ${MERGE_QUEUE}"
    local f idle=0
    while worker_is_me; do
        f="$(first_entry)"
        if [ -z "$f" ]; then
            if [ -n "$WORKER_IDLE_MAX_SEC" ] && [ "$idle" -ge "$WORKER_IDLE_MAX_SEC" ]; then
                echo "pool: очередь пуста дольше ${WORKER_IDLE_MAX_SEC} с — воркер закончил"
                return 0
            fi
            sleep "$WORKER_INTERVAL_SEC"
            idle=$((idle + $(step_of "$WORKER_INTERVAL_SEC")))
            continue
        fi
        idle=0
        take_merging || break
        if ! merge_entry "$f"; then
            log "${f##*-}" "GitHub или git не ответили — повторю"
            sleep "$WORKER_INTERVAL_SEC"
        fi
        leave_merging
    done
    echo "pool: воркер слияния остановлен — его замок снят (release или закрыто окно-диспетчер)"
}

cmd_merge_enter() {
    need_owner
    my_slot >/dev/null || die "сначала номер окна: ops/pool.sh slot"
    if is_mine "$POOL_DIR/merging"; then echo "pool: замок слияния уже твой"; return 0; fi
    if worker_alive; then
        echo "pool: воркер слияния работает — сам не мержи: ops/pool.sh enqueue <PR>" >&2
        exit 1
    fi
    local waited=0
    until acquire merging; do
        if [ "$waited" -ge "$MERGE_ENTER_TIMEOUT_SEC" ]; then
            echo "pool: замок слияния занят уже ${waited} с — повтори ops/pool.sh merge-enter" >&2
            exit 4
        fi
        sleep "$MERGE_ENTER_INTERVAL_SEC"
        waited=$((waited + $(step_of "$MERGE_ENTER_INTERVAL_SEC")))
    done
    echo "pool: замок слияния твой — обнови PR до main, дождись check, approve, merge; потом ops/pool.sh merge-leave"
}

cmd_merge_leave() {
    need_owner
    if is_mine "$POOL_DIR/merging"; then
        rm -rf "$POOL_DIR/merging"
        echo "pool: замок слияния свободен"
        return 0
    fi
    echo "pool: замок слияния не твой (снят как зависший?) — проверь make pool" >&2
    exit 1
}

merge_status() {
    local f names="" d="$POOL_DIR/merging" who
    if worker_alive; then
        echo "    воркер слияния: работает — после approve ops/pool.sh enqueue <PR>"
    else
        echo "    воркер слияния: нет — окна мержат сами, по одному: merge-enter / merge-leave"
    fi
    for f in "$MERGE_QUEUE"/*-*; do
        if [ -f "$f" ]; then names="${names} #${f##*-}"; fi
    done
    if [ -n "$names" ]; then echo "    очередь слияния:${names}"; fi
    if [ -d "$d" ]; then
        who="окно $(window_of "$(lock_owner "$d")")"
        if worker_alive && [ "$(lock_owner "$d")" = "$(lock_owner "$POOL_DIR/merge-worker")" ]; then who="воркер"; fi
        echo "    слияние идёт: ${who}, $(lock_state "$d")"
    fi
}
