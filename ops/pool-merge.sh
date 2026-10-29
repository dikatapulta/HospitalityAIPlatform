# shellcheck shell=bash
# Конвейер слияния пула окон (решение основателя 28.10): очередь и воркер без модели;
# без воркера тот же разбор одного PR делает merge-one в окне, под замком слияния.
# Подключается из ops/pool.sh — замки и владелец там. Когда скрипт вправе повторить
# approve — правило 9 PROJECT_EXECUTION_PLAN («Merge»); шаги окон — docs/QUEUE.md,
# «Пул окон». Тесты — tests/test_pool_merge.py и tests/test_pool_merge_guard.py.
#
# Запись очереди — файл merge-queue/<ГГГГММДДччммсс>-<PR> со строкой «PR одобренная»:
# commit, на который ревьюер поставил approve. Влитый main меняет голову, но не
# содержимое: голова — потомок одобренной, и её дерево совпадает с деревом «main,
# влитый в одобренную» (git merge-tree). Любая другая правка, хоть пробел или перенос
# строки, дерево меняет — и повторного approve нет.

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
MERGED=0
# Подписи комментариев и журнала — воркера; merge-one ставит свои.
SOURCE="Очередь слияния" STOPPED="PR снят" DROP_WORD="снят из очереди" AGAIN="ops/pool.sh enqueue"

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

# --- git: содержимое PR ----------------------------------------------------------

fetch_pr() { # свежий main и голова PR $1 в локальном git; $2 — ожидаемая голова
    git fetch -q origin main "refs/pull/$1/head" 2>/dev/null && git cat-file -e "$2^{commit}" 2>/dev/null
}

# Дерево «origin/main, влитый в $1» — первая строка git merge-tree. Код 1 — конфликт,
# другой ненулевой — сбой git.
merged_tree() {
    local out rc=0
    out="$(git merge-tree --write-tree refs/remotes/origin/main "$1" 2>/dev/null)" || rc=$?
    [ "$rc" = 0 ] || return "$rc"
    echo "${out%%$'\n'*}"
}

# Голова $2 — одобренная $1 с влитым main и ничего больше: потомок одобренной, и её
# дерево с main ($3 — merged_tree головы) совпадает с деревом одобренной с main.
# 0 — то же содержимое; 1 — другое (или история переписана); 2 — сбой git.
same_content() {
    local tree rc=0
    [ "$1" = "$2" ] && return 0
    git merge-base --is-ancestor "$1" "$2" 2>/dev/null || return 1
    tree="$(merged_tree "$1")" || rc=$?
    case "$rc" in 0) ;; 1) return 1 ;; *) return 2 ;; esac
    [ "$tree" = "$3" ]
}

# --- GitHub ----------------------------------------------------------------------

# «состояние черновик решение вердикт одобренная голова». Вердикт — последний review,
# кроме комментариев (снятый approve — DISMISSED); одобренная — commit последнего
# живого approve. Стоп по request changes берётся из вердикта, а не из reviewDecision:
# снятый approve GitHub считает комментарием, и решение может вернуть старый request
# changes, уже исправленный автором, — тогда снимался бы каждый PR после доработки.
pr_state() {
    gh pr view "$1" --json state,isDraft,reviewDecision,reviews,headRefOid --jq '
        def v: if . == null or . == "" then "NONE" else . end;
        ([.reviews[] | select(.state != "COMMENTED" and .state != "PENDING")] | sort_by(.submittedAt)) as $r
        | "\(.state) \(.isDraft) \(.reviewDecision | v) \($r | last | .state | v) \([$r[] | select(.state == "APPROVED")] | last | .commit.oid | v) \(.headRefOid)"'
}

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
        if out="$(pr_state "$1" 2>/dev/null)" && [ "${out##* }" != "$2" ]; then return 0; fi
        sleep "$CHECK_INTERVAL_SEC"
        waited=$((waited + step))
    done
}

approve_unchanged() { # PR голова одобренная — при отказе скрипт останавливается (код 5)
    local text try
    text="Обновление до main, содержимое не менялось: голова ${2:0:8} — потомок одобренной ${3:0:8}, и её дерево совпадает с деревом «main, влитый в ${3:0:8}» (git merge-tree); required check \`check\` зелёный. Повторный approve — ${SOURCE} (ops/pool.sh), правило 9."
    for try in 1 2; do
        if reviewer_review "$1" "$2" APPROVE "$text"; then return 0; fi
        [ "$try" = 2 ] || sleep "$WORKER_INTERVAL_SEC"
    done
    log "$1" "повторный approve аккаунтом ревьюера не прошёл (${REVIEWER_GH_CONFIG}; механика — docs/runbooks/review-process.md) — остановлен, PR не смержен (запись очереди остаётся)"
    exit 5
}

# --- один PR ---------------------------------------------------------------------

drop() { # запись снята; PR $2, причина $3
    rm -f "$1"
    log "$2" "${DROP_WORD} — $3"
}

stop_pr() { # запись снята с комментарием в PR: файл, PR, причина для журнала, текст комментария
    pr_comment "$2" "${SOURCE}: ${STOPPED} — $4"
    drop "$1" "$2" "$3"
}

# Живое состояние PR → PR_HEAD и PR_DECISION. 0 — вести дальше; 2 — запись снята:
# PR закрыт, черновик, request changes или approve снят руками — так человек и
# останавливает PR, approve скрипта это не перекрывает; 1 — GitHub не ответил.
read_pr() { # файл PR одобренная
    local out state draft verdict reason
    out="$(pr_state "$2")" || return 1
    read -r state draft PR_DECISION verdict _ PR_HEAD <<<"$out"
    if [ "$state" != OPEN ]; then drop "$1" "$2" "PR уже ${state}"; return 2; fi
    if [ "$draft" = true ]; then
        reason="черновик"
    elif [ "$verdict" = CHANGES_REQUESTED ]; then
        reason="request changes"
    elif [ "$PR_HEAD" = "$3" ] && [ "$PR_DECISION" != APPROVED ]; then
        reason="approve снят"
    else
        return 0
    fi
    stop_pr "$1" "$2" "$reason" "${reason}: approve и merge скрипт не делает. После нового approve — снова ${AGAIN} ${2}."
    return 2
}

# 0 — запись разобрана (смержен или снят), 1 — сбой сети или git: повторить позже.
merge_entry() {
    local file="$1" pr approved tree checked st err rc attempt=0
    read -r pr approved <"$file" || { drop "$file" "?" "запись не читается"; return 0; }
    while :; do
        attempt=$((attempt + 1))
        if [ "$attempt" -gt "$MERGE_ATTEMPTS" ]; then
            stop_pr "$file" "$pr" "попытки кончились" "за ${MERGE_ATTEMPTS} попыток не смержился (main уходит вперёд или GitHub отказывает). Повтори: ${AGAIN} ${pr}."
            return 0
        fi
        rc=0 && read_pr "$file" "$pr" "$approved" || rc=$?
        case "$rc" in 0) ;; 2) return 0 ;; *) return 1 ;; esac
        fetch_pr "$pr" "$PR_HEAD" || return 1
        rc=0 && tree="$(merged_tree "$PR_HEAD")" || rc=$?
        if [ "$rc" = 1 ]; then
            st="${SOURCE}: ${STOPPED} — нужна доработка: конфликт с main. Влей main в ветку, разреши конфликт, затем повторный проход ревью."
            reviewer_review "$pr" "$PR_HEAD" REQUEST_CHANGES "$st" 2>/dev/null || pr_comment "$pr" "$st"
            drop "$file" "$pr" "конфликт с main"
            return 0
        fi
        [ "$rc" = 0 ] || return 1
        rc=0 && same_content "$approved" "$PR_HEAD" "$tree" || rc=$?
        if [ "$rc" = 1 ]; then
            stop_pr "$file" "$pr" "содержимое изменилось после approve" "нужен повторный проход. После approve на ${approved:0:8} в ветку попало не только слияние с main: голова ${PR_HEAD:0:8} — не одобренная с влитым main (другое дерево или переписанная история). После нового approve — снова ${AGAIN} ${pr}."
            return 0
        fi
        [ "$rc" = 0 ] || return 1
        if ! git merge-base --is-ancestor refs/remotes/origin/main "$PR_HEAD"; then
            if ! err="$(gh pr update-branch "$pr" 2>&1)"; then
                log "$pr" "update-branch не прошёл: ${err}"
                sleep "$WORKER_INTERVAL_SEC"
                continue
            fi
            log "$pr" "отставал от main — обновлён"
            wait_moved "$pr" "$PR_HEAD"
            continue
        fi
        checked="$PR_HEAD"
        st="$(wait_check "$pr" "$checked")"
        case "$st" in
        success) ;;
        moved) continue ;;
        timeout)
            stop_pr "$file" "$pr" "check не дождался" "required check \`check\` не закончился за $((CHECK_MAX_SEC / 60)) мин на ${checked:0:8}. Посмотри Actions; зелёный — снова ${AGAIN} ${pr}."
            return 0
            ;;
        *)
            stop_pr "$file" "$pr" "check: ${st}" "нужна доработка: required check \`check\` красный (${st}) на ${checked:0:8}."
            return 0
            ;;
        esac
        # check ждали до 40 минут: перед approve и merge — снова живое состояние PR.
        rc=0 && read_pr "$file" "$pr" "$approved" || rc=$?
        case "$rc" in 0) ;; 2) return 0 ;; *) return 1 ;; esac
        [ "$PR_HEAD" = "$checked" ] || continue
        # Голова — одобренная с влитым main (сверено выше): approve снят GitHub'ом, его
        # повторяет скрипт; уже стоит на этой голове (прошлая попытка merge) — не повторяет.
        if [ "$checked" != "$approved" ] && [ "$PR_DECISION" != APPROVED ]; then
            approve_unchanged "$pr" "$checked" "$approved"
        fi
        if err="$(gh pr merge "$pr" -R "$REPO" --squash --delete-branch --match-head-commit "$checked" 2>&1)"; then
            MERGED=1
            drop "$file" "$pr" "смержен"
            return 0
        fi
        log "$pr" "merge не прошёл: ${err}"
        sleep "$WORKER_INTERVAL_SEC"
    done
}

# --- команды ---------------------------------------------------------------------

pr_number() { case "$1" in '' | *[!0-9]*) die "PR — номер, а не «${1}»" ;; esac; }

# Одобренная — commit, на который поставлен живой approve (не голова: approve мог
# ещё не сняться после push). Нет живого approve — отказ, код 1.
approved_of() {
    local out state draft decision verdict approved
    out="$(pr_state "$1")" || die "GitHub не ответил о PR #${1}"
    read -r state draft decision verdict approved _ <<<"$out"
    if [ "${state} ${draft} ${decision} ${verdict}" != "OPEN false APPROVED APPROVED" ]; then
        echo "pool: PR #${1} — ${state}, черновик: ${draft}, решение: ${decision}, последний вердикт: ${verdict}; нужен открытый PR с живым approve" >&2
        exit 1
    fi
    echo "$approved"
}

cmd_enqueue() {
    [ $# -eq 1 ] || die "использование: ops/pool.sh enqueue <PR>"
    pr_number "$1"
    need_owner
    if ! worker_alive; then
        echo "pool: воркера слияния нет — мержи сам: ops/pool.sh merge-one ${1} (в фоне)" >&2
        exit 1
    fi
    local approved tmp
    approved="$(approved_of "$1")" || exit $?
    mkdir -p "$MERGE_QUEUE"
    rm -f "$MERGE_QUEUE"/*-"$1"
    tmp="$MERGE_QUEUE/.new.$$"
    printf '%s %s\n' "$1" "$approved" >"$tmp"
    mv "$tmp" "$MERGE_QUEUE/$(date '+%Y%m%d%H%M%S')-$1"
    echo "pool: PR #${1} в очереди слияния (одобрен ${approved:0:8}) — воркер обновит его до main, дождётся check и смержит"
}

# Без воркера: тот же разбор одного PR, что у воркера, — синхронно, под замком слияния.
# Повторный approve ставит только он (правило 9), окно руками — нет.
cmd_merge_one() {
    [ $# -eq 1 ] || die "использование: ops/pool.sh merge-one <PR>"
    pr_number "$1"
    need_owner
    wait_merging
    trap leave_merging EXIT
    REPO="$(gh repo view --json nameWithOwner --jq .nameWithOwner)" || die "gh не ответил"
    SOURCE="Слияние окна (ops/pool.sh merge-one)" STOPPED="PR не смержен" DROP_WORD="итог"
    AGAIN="ops/pool.sh merge-one"
    local approved entry rc=0
    approved="$(approved_of "$1")" || exit $?
    entry="$(mktemp "${TMPDIR:-/tmp}/pool-merge-one.XXXXXX")"
    printf '%s %s\n' "$1" "$approved" >"$entry"
    merge_entry "$entry" || rc=$?
    rm -f "$entry"
    if [ "$rc" != 0 ]; then
        echo "pool: GitHub или git не ответили — повтори ops/pool.sh merge-one ${1}" >&2
        exit 1
    fi
    if [ "$MERGED" = 1 ]; then echo "pool: PR #${1} смержен"; return 0; fi
    echo "pool: PR #${1} не смержен — причина строкой выше и в комментарии PR" >&2
    exit 1
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

wait_merging() { # окно ждёт замок слияния: воркер жив — код 1, не дождался — код 4
    my_slot >/dev/null || die "сначала номер окна: ops/pool.sh slot"
    if is_mine "$POOL_DIR/merging"; then HOLD_MERGING=1; return 0; fi
    if worker_alive; then
        echo "pool: воркер слияния работает — сам не мержи: ops/pool.sh enqueue <PR>" >&2
        exit 1
    fi
    local waited=0
    until acquire merging; do
        if [ "$waited" -ge "$MERGE_ENTER_TIMEOUT_SEC" ]; then
            echo "pool: замок слияния занят уже ${waited} с — повтори" >&2
            exit 4
        fi
        sleep "$MERGE_ENTER_INTERVAL_SEC"
        waited=$((waited + $(step_of "$MERGE_ENTER_INTERVAL_SEC")))
    done
    HOLD_MERGING=1
}

cmd_merge_enter() {
    need_owner
    if is_mine "$POOL_DIR/merging"; then echo "pool: замок слияния уже твой"; return 0; fi
    wait_merging
    echo "pool: замок слияния твой — merge-one возьмёт его без ожидания; approve руками не ставить; отпустить — ops/pool.sh merge-leave"
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
        echo "    воркер слияния: нет — после approve окно мержит само: ops/pool.sh merge-one <PR>"
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
