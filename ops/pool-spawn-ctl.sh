# shellcheck shell=bash
# Управление сторожем безголовых окон (#588): пауза, «освободи место», предел окон,
# отметка «брать нечего» и строки сторожа в make pool. Подключается из ops/pool.sh после
# ops/pool-spawn.sh — записи окон и сам сторож там. Слова основателя, которыми диспетчер
# вызывает эти команды, — docs/QUEUE.md, «Пул окон». Тесты — tests/test_pool_spawn.py.

# Порядок остановки (#588): сначала окна, ждущие ответа или работы (`ops/pool.sh wait`
# среди их процессов), потом — от самого молодого по времени старта. -ww: без него
# Linux-ps в трубе режет строку по COLUMNS, и «pool.sh wait» в ней не видно (CI PR #589).
waiting_pids() {
    local pids=" " id
    for id in $(ids_in_state running); do pids="${pids}$(rec_get "$id" pid) "; done
    ps -ww -A -o pid=,ppid=,args= 2>/dev/null | awk -v w="$pids" '
        { par[$1] = $2; if ($0 ~ /pool\.sh wait/) cand[$1] = 1 }
        END { for (p in cand) { q = p; for (i = 0; i < 40 && q > 1; i++) {
            if (index(w, " " q " ")) { print q; break }; q = par[q] } } }'
}

stop_order() {
    local waiting id
    waiting=" $(waiting_pids | tr '\n' ' ') "
    for id in $(ids_in_state running); do
        case "$waiting" in *" $(rec_get "$id" pid) "*) echo "0 $(rec_get "$id" started) $id" ;;
        *) echo "1 $(rec_get "$id" started) $id" ;; esac
    done | sort -k1,1n -k2,2nr | awk '{ print $3 }'
}

stop_workers() { # id… — SIGINT (ход завершается), через STOP_GRACE_SEC — SIGTERM
    local id pid waited=0 left
    for id in "$@"; do
        : >"$WORKERS/$id/stop"
        pid="$(rec_get "$id" pid)"
        if alive "$pid"; then kill -INT "$pid" 2>/dev/null || true; fi
        echo "pool: окно ${id} — остановлено (продолжит после ops/pool.sh resume)"
    done
    while :; do
        left=""
        for id in "$@"; do
            if alive "$(rec_get "$id" pid)"; then left="${left} ${id}"; fi
        done
        [ -n "$left" ] || return 0
        if [ "$waited" -ge "$STOP_GRACE_SEC" ]; then break; fi
        sleep 1
        waited=$((waited + 1))
    done
    for id in $left; do kill -TERM "$(rec_get "$id" pid)" 2>/dev/null || true; done
}

cmd_pause() {
    case "${1:-}" in '' | --now) ;; *) die "использование: ops/pool.sh pause [--now]" ;; esac
    mkdir -p "$WORKERS"
    : >"$PAUSE_FLAG"
    echo "pool: пауза — новых окон сторож не запускает; продолжить — ops/pool.sh resume"
    if [ "${1:-}" = --now ]; then
        # shellcheck disable=SC2046 # список id — слова без пробелов
        stop_workers $(ids_in_state running)
    fi
}

cmd_make_room() {
    local k="${1:-1}" ids
    case "$k" in '' | 0 | *[!0-9]*) die "использование: ops/pool.sh make-room [сколько окон, по умолчанию 1]" ;; esac
    mkdir -p "$WORKERS"
    : >"$PAUSE_FLAG"
    ids="$(stop_order | awk -v k="$k" 'NR <= k')"
    echo "pool: пауза — новых окон сторож не запускает; продолжить — ops/pool.sh resume"
    if [ -z "$ids" ]; then echo "pool: безголовых окон нет — останавливать нечего"; return 0; fi
    # shellcheck disable=SC2086 # список id — слова без пробелов
    stop_workers $ids
}

cmd_resume() { # «продолжай» основателя снимает и паузу, и простой после лимита или обрывов
    rm -f "$PAUSE_FLAG" "$WORKERS/.hold"
    echo "pool: пауза снята — сторож продолжит остановленные окна (--resume), затем новые"
}

cmd_windows() { # «запускай не больше N окон»: работающие сверх предела дорабатывают
    local n="${1:-}"
    mkdir -p "$WORKERS"
    if [ -n "$n" ]; then
        case "$n" in 0 | *[!0-9]*) die "использование: ops/pool.sh windows [N от 1 до ${POOL_SLOTS}]" ;; esac
        if [ "$n" -ge "$POOL_SLOTS" ]; then rm -f "$WORKERS/.cap"; else printf '%s\n' "$n" >"$WORKERS/.cap"; fi
    fi
    echo "pool: сторож держит не больше $(cap) окон из ${POOL_SLOTS}; работающие сверх предела дорабатывают"
}

cmd_idle() {
    need_owner
    local id
    id="$(worker_of "$OWNER")" || die "это не безголовое окно сторожа — ручному окну брать нечего: ops/pool.sh wait work"
    : >"$WORKERS/$id/idle"
    echo "pool: отмечено «брать нечего» — ops/pool.sh release и закончи; сторож запустит окно снова, когда в пуле что-то изменится"
}

spawn_status() {
    local pause="нет" id
    if [ -f "$PAUSE_FLAG" ]; then pause="да"; fi
    if spawner_alive; then
        echo "    сторож: работает, окон $(ids_in_state running | wc -l | tr -d ' '), предел $(cap), пауза ${pause}"
        case "$(memory_level)" in
        1) ;;
        4) echo "    сторож: память critical — новых окон нет" ;;
        *) echo "    сторож: память не normal — новые окна только до ${MIN_WINDOWS}" ;;
        esac
    else
        echo "    сторож: нет — окна открываются руками (окно-диспетчер: ops/pool.sh spawner), предел $(cap), пауза ${pause}"
    fi
    if held; then echo "    сторож: стоит до $(clock "$(held_till)" '%d.%m %H:%M') (лимит подписки или обрывы; снять — ops/pool.sh resume)"; fi
    for id in $(ids_in_state want); do
        echo "    безголовое окно ${id}: ждёт возобновления — $(rec_get "$id" want)"
    done
    perm_status
}

perm_status() { # журнал ops/pool-perm-hook.sh: отказы безголовым окнам за сегодня
    local log="$WORKERS/permissions.log" n
    [ -f "$log" ] || return 0
    n="$(awk -v d="$(date '+%d.%m') " 'index($0, d) == 1' "$log" | wc -l | tr -d ' ')"
    echo "    разрешения безголовых окон: сегодня отказов ${n} — ${log}"
}

spawn_cmd() { # команды сторожа — из разбора команд ops/pool.sh
    local c="$1"
    shift
    case "$c" in
    spawner) cmd_spawner ;;
    pause) cmd_pause "$@" ;;
    resume) cmd_resume ;;
    make-room) cmd_make_room "$@" ;;
    windows) cmd_windows "$@" ;;
    idle) cmd_idle ;;
    _run) cmd_spawn_run "$@" ;;
    esac
}
