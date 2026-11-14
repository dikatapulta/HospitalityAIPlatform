# shellcheck shell=bash
# Подключается из ops/pool.sh после ops/pool-spawn.sh — сам сторож, его ход и метки
# .idle-<kind> там (вынесено из него, #1109). Тесты — tests/test_pool_spawn_work.py.
#
# Есть ли что брать — без модели, перед запуском окна (#743). Замер 02–03.11: 237 из 437 окон
# ушли со словами «брать нечего» — снимок `wait work` меняется от любого шага занятых окон,
# а работы от этого не прибавляется. Сторож сверяет то, что окно прочло бы в турникете:
# раздел 1 — разбором ops/queue.sh --free (тем же, что у make queue), заявки и очередь
# слияния — замками пула. Проверка не удалась — окно запускается, как до неё: лишнее окно
# дешевле вставшего пула. Гоняется, только когда сторож и так собрался запускать окно.
QUEUE_SRC="${POOL_QUEUE_FILE:-}" # пусто — docs/QUEUE.md свежего origin/main (тесты дают файл)
WORK=""
WORK_RC="" # пусто — в этом ходу ещё не спрашивали

free_work() { # вывод ops/queue.sh --free в WORK — один раз за ход; 1 — не удалось
    local q="$QUEUE_SRC"
    if [ -z "$WORK_RC" ]; then
        WORK_RC=1
        if [ -z "$q" ]; then
            q="$WORKERS/.queue.md"
            git -C "$ROOT" fetch -q origin main >/dev/null 2>&1 || true # не вышло — какой origin/main есть
            git -C "$ROOT" show origin/main:docs/QUEUE.md >"$q" 2>/dev/null || return 1
        fi
        if WORK="$(QUEUE_FILE="$q" bash "${SELF%/*}/queue.sh" --free 2>/dev/null)"; then WORK_RC=0; fi
    fi
    return "$WORK_RC"
}

claim_busy() { [ -d "$POOL_DIR/$1" ] && ! lock_stale "$POOL_DIR/$1"; }

in_merge_queue() {
    local f
    for f in "$MERGE_QUEUE"/*-"$1"; do if [ -f "$f" ]; then return 0; fi; done
    return 1
}

# PR ждёт ревью, доработки или merge, и его никто не взял — пропуски шага 2 «Рабочего окна».
pr_wants_window() { # PR состояние
    case "$2" in REVIEW_REQUIRED | REWORK_PUSHED | DOCS_CONFLICT | CHANGES_REQUESTED | APPROVED) ;; *) return 1 ;; esac
    ! in_merge_queue "$1" && ! claim_busy "review-$1" && ! claim_busy "rework-$1" && ! claim_busy "merge-$1"
}

work_for() { # kind → 0 есть что брать; 1 нечего; 2 проверка не удалась
    local what n rest
    free_work || return 2
    while read -r what n rest; do
        case "$what" in
        item) if [ "$rest" = "$1" ]; then return 0; fi ;; # rest — модель пункта
        pr) if [ "$1" = opus ] && pr_wants_window "$n" "$rest"; then return 0; fi ;;
        esac
    done <<<"$WORK"
    return 1
}

# 0 — запускать окно kind; 1 — нечего: метка .idle-<kind>, как от окна, ушедшего с «брать нечего».
has_work() {
    local rc=0
    work_for "$1" || rc=$?
    case "$rc" in
    0) return 0 ;;
    1) printf '%s\n' "$SNAP" >"$WORKERS/.idle-$1"; return 1 ;;
    *) say "проверка «есть что брать» не удалась — окно ${1} запускаю, как раньше"; return 0 ;;
    esac
}
