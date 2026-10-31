# shellcheck shell=bash
# Чистка копий репозитория (#585, решение основателя 31.10): окна пула и сессии
# с EnterWorktree оставляют копии в .claude/worktrees/, у многих своя .venv — к 31.10
# их было 182 на 36 ГБ. Подключается из ops/pool.sh; кто и когда запускает —
# docs/QUEUE.md, «Пул окон». Тесты — tests/test_pool_clean.py.
#
# Без флага — сухой прогон: копия, вердикт и причина. Удаляет только --yes, и только
# копию, о которой нет ни одного сомнения: удаление необратимо, а лишняя оставленная
# копия стоит лишь места на диске. Удаляется папка, ветка копии остаётся в репозитории.
#
# Оставить, если верно хоть одно (проверки — в clean_verdict, по порядку):
# - замок не от сессии Claude или от живой («claude session … (pid N start …)» — так
#   Claude Code запирает копию, пока сессия жива; `claude -p` замок не снимает, поэтому
#   замок с мёртвым pid — брошенный);
# - копия безголового окна pool-<id>, а запись workers/<id> у сторожа ещё есть: окно
#   могут возобновить, и без копии оно работало бы в главной папке;
# - рабочая папка живого процесса внутри копии (сессия, uvicorn из /verify, pytest);
# - копия моложе часа: сессия могла создать её и ещё не запереть;
# - незакоммиченные изменения или коммиты, которых нет ни в одной ветке origin.

CLEAN_MIN_AGE_MIN=60
CLEAN_BASE=""
CLEAN_CWDS=""

# Рабочие папки процессов пользователя: «pid<TAB>имя<TAB>путь» по строке. lsof — macOS,
# /proc — Linux без lsof. Своего процесса в списке нет — список не верен, и копии
# не трогаются вовсе: без него живую сессию не отличить от брошенной копии.
process_cwds() {
    local out d
    if command -v lsof >/dev/null 2>&1; then
        out="$(lsof -w -d cwd -Fpcn 2>/dev/null || true)"
        out="$(awk '/^p/ { p = substr($0, 2) } /^c/ { c = substr($0, 2) }
            /^n/ { print p "\t" c "\t" substr($0, 2) }' <<<"$out")"
    elif [ -d /proc/self ]; then
        out="$(for d in /proc/[0-9]*; do
            printf '%s\t%s\t%s\n' "${d#/proc/}" "$(cat "$d/comm" 2>/dev/null || echo '?')" \
                "$(readlink "$d/cwd" 2>/dev/null || true)"
        done)"
    else
        return 1
    fi
    awk -F'\t' -v me="$$" '$1 == me { found = 1 } END { exit !found }' <<<"$out" || return 1
    printf '%s\n' "$out"
}

# Копии под $1: «путь<TAB>HEAD<TAB>замок» по строке; замок «-» — не заперта, иначе
# причина («locked», если её нет).
worktree_rows() {
    local list
    list="$(git -c core.quotePath=false worktree list --porcelain)" || return 1 # кириллица причины — как есть
    awk -v base="$1/" '
        function out() { if (index(p, base) == 1) print p "\t" h "\t" l }
        /^worktree / { if (p != "") out(); p = substr($0, 10); h = ""; l = "-" }
        /^HEAD / { h = substr($0, 6) }
        /^locked$/ { l = "locked" }
        /^locked / { l = substr($0, 8) }
        END { if (p != "") out() }' <<<"$list"
}

lock_session_pid() { sed -nE 's/^claude session .* \(pid ([0-9]+) start .*\)$/\1/p' <<<"$1"; }

# Путь, HEAD, замок → «-» (удалить) или причина оставить. Ошибка любой проверки — причина,
# а не «-»: функция выполняется в $(…), где set -e не действует.
clean_verdict() {
    local wt="$1" head="$2" lock="$3" name pid real busy dirty ahead
    name="${wt#"$CLEAN_BASE"/}"
    if [ "$lock" != "-" ]; then
        pid="$(lock_session_pid "$lock")"
        if [ -z "$pid" ]; then echo "заперта: ${lock}"; return; fi
        if ps -p "$pid" -o pid= >/dev/null 2>&1; then echo "живая сессия Claude (pid ${pid})"; return; fi
    fi
    case "$name" in
    pool-*)
        if [ -d "$WORKERS/${name#pool-}" ]; then
            echo "окно сторожа ${name#pool-} ещё на учёте — его могут возобновить"
            return
        fi
        ;;
    esac
    if [ -e "$wt" ]; then # папки нет — снимется только запись, если HEAD на origin
        if ! real="$(cd "$wt" 2>/dev/null && pwd -P)"; then echo "папка не читается"; return; fi
        busy="$(awk -F'\t' -v w="$real" '!found && ($3 == w || index($3, w "/") == 1) {
            print $2 " (pid " $1 ")"; found = 1 }' <<<"$CLEAN_CWDS")"
        if [ -n "$busy" ]; then echo "в ней работает процесс ${busy}"; return; fi
        if [ -n "$(find "$wt" -maxdepth 0 -mmin -"$CLEAN_MIN_AGE_MIN" 2>/dev/null)" ]; then
            echo "создана или тронута меньше ${CLEAN_MIN_AGE_MIN} мин назад"
            return
        fi
        # --no-optional-locks: не обновлять индекс чужой копии — её сессия могла бы
        # споткнуться о наш index.lock.
        if ! dirty="$(git --no-optional-locks -C "$wt" status --porcelain 2>/dev/null)"; then
            echo "git status не прочитался"
            return
        fi
        if [ -n "$dirty" ]; then echo "незакоммиченные изменения"; return; fi
    fi
    if ! ahead="$(git rev-list -1 "$head" --not --remotes=origin 2>/dev/null)"; then
        echo "не проверил коммиты HEAD"
        return
    fi
    if [ -n "$ahead" ]; then echo "коммиты, которых нет на origin"; return; fi
    echo "-"
}

human_size() { # килобайты → «N МБ» или «X.Y ГБ»
    awk -v k="$1" 'BEGIN { if (k < 1048576) printf "%d МБ", k / 1024; else printf "%.1f ГБ", k / 1048576 }'
}

disk_free_kb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 { print $4 }'; }

cmd_clean_worktrees() {
    local yes=0 root rows row wt head lock name verdict kb err
    local -a force
    local total=0 kept=0 picked=0 picked_kb=0 removed=0 freed_kb=0 failed=0
    case "$#:${1:-}" in
    0:) ;;
    1:--yes) yes=1 ;;
    *) die "использование: ops/pool.sh clean-worktrees [--yes]" ;;
    esac
    root="$(main_root)" || die "не нашёл корень репозитория"
    CLEAN_BASE="$root/.claude/worktrees"
    CLEAN_CWDS="$(process_cwds)" ||
        die "не прочитал рабочие папки процессов (lsof) — без них живую сессию не отличить, копии не трогаю"
    rows="$(worktree_rows "$CLEAN_BASE")" || die "git worktree list не прочитался"
    if [ "$yes" = 1 ]; then
        echo "==> Копии репозитория в ${CLEAN_BASE}: удаляю те, о которых нет сомнений"
    else
        echo "==> Копии репозитория в ${CLEAN_BASE}: сухой прогон, ничего не удаляю"
    fi
    while IFS=$'\t' read -r wt head lock <&3; do
        [ -n "$wt" ] || continue
        total=$((total + 1))
        name="${wt#"$CLEAN_BASE"/}"
        verdict="$(clean_verdict "$wt" "$head" "$lock")"
        # Прогон --yes длится минуты: перед удалением — снова, со свежими HEAD, замком
        # и процессами, чтобы не удалить копию, в которую сессия вошла за это время.
        if [ "$yes" = 1 ] && [ "$verdict" = "-" ]; then
            CLEAN_CWDS="$(process_cwds)" || die "не прочитал рабочие папки процессов — дальше копии не трогаю"
            row="$(worktree_rows "$CLEAN_BASE" | awk -F'\t' -v p="$wt" '$1 == p')"
            verdict="пропала из git worktree list"
            if [ -n "$row" ]; then
                IFS=$'\t' read -r wt head lock <<<"$row"
                verdict="$(clean_verdict "$wt" "$head" "$lock")"
            fi
        fi
        if [ "$verdict" != "-" ]; then
            kept=$((kept + 1))
            printf '    оставить  %-32s %s\n' "$name" "${verdict:-не проверено}"
            continue
        fi
        kb="$(du -sk "$wt" 2>/dev/null | awk '{ print $1 }' || true)"
        kb="${kb:-0}"
        # Второй --force снимает замок — только брошенный, проверенный выше; копию,
        # запертую после проверки, git с одним --force удалить откажется.
        force=(--force)
        if [ "$lock" != "-" ]; then force+=(--force); fi
        if [ "$yes" = 0 ]; then
            picked=$((picked + 1)) picked_kb=$((picked_kb + kb))
            printf '    удалить   %-32s %s\n' "$name" "$(human_size "$kb")"
        elif err="$(git worktree remove "${force[@]}" "$wt" 2>&1)"; then
            removed=$((removed + 1)) freed_kb=$((freed_kb + kb))
            printf '    удалено   %-32s %s\n' "$name" "$(human_size "$kb")"
        else
            failed=$((failed + 1))
            printf '    ошибка    %-32s %s\n' "$name" "$(awk 'NR == 1' <<<"$err")"
        fi
    done 3<<<"$rows"
    if [ "$yes" = 0 ]; then
        echo "==> Копий ${total}: удалить ${picked} (≈ $(human_size "$picked_kb")), оставить ${kept}."
        if [ "$picked" -gt 0 ]; then echo "    Удалить: ops/pool.sh clean-worktrees --yes"; fi
        return 0
    fi
    git worktree prune || true
    echo "==> Удалено копий ${removed}, освобождено $(human_size "$freed_kb"); оставлено ${kept}; на диске свободно $(human_size "$(disk_free_kb "$root")")."
    if [ "$failed" -gt 0 ]; then
        echo "    Не удалилось ${failed} — причина в строках «ошибка»; копии не тронуты, повтори позже." >&2
        return 1
    fi
}
