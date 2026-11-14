# shellcheck shell=bash
# Надзор за процессами окна-диспетчера (#1057 п. 3). 11.11 среда выполнения сняла фоновые
# команды диспетчера при нехватке памяти — воркер слияния и сторож, — и пул стоял 3 ч 51 мин.
# Поэтому `ops/pool.sh start` запускает обоих отцепленными процессами (detach — как окна
# сторожа, ops/pool-spawn.sh) от имени диспетчера, и каждый на своём ходу поднимает упавшего
# соседа: сторож — воркер, воркер — сторожа. Живут они, как и прежде, пока живо окно-диспетчер.
# Воркер, остановленный кодом 5 (повторный approve аккаунтом ревьюера не прошёл), не
# поднимается — нужны руки основателя. Очередь не пуста, а воркера нет дольше
# POOL_NO_WORKER_ALERT_MIN минут — строка «!!» в make pool и одна запись в журнале
# основателя (она будит `wait ask` диспетчера). Подключается из ops/pool.sh после
# ops/pool-spawn.sh и ops/pool-merge.sh. Тесты — tests/test_pool_supervise.py.

SUPERVISE="${POOL_SUPERVISE:-0}" # 1 — процесс запущен `start` и поднимает соседа
NO_WORKER_ALERT_MIN="${POOL_NO_WORKER_ALERT_MIN:-30}"
START_WAIT_SEC="${POOL_START_WAIT_SEC:-15}"
WORKER_BEAT="$POOL_DIR/merge-worker.beat" # воркер жив — касается на каждом ходу
ALERTED="$POOL_DIR/.no-worker-alert"      # запись о простое уже в журнале

bg_alive() { if [ "$1" = merge-worker ]; then worker_alive; else spawner_alive; fi; }

bg_start() { # имя — отцепленным процессом от имени окна-диспетчера, лог <имя>.log
    rm -f "$POOL_DIR/$1.exit"
    detach env POOL_OWNER_PID="$OWNER" POOL_SUPERVISE=1 bash "$SELF" _bg "$1"
}

# Внутренняя: обёртка отцепленного процесса — его код выхода в <имя>.exit.
cmd_bg_run() {
    local rc=0
    case "${1:-}" in merge-worker | spawner) ;; *) die "нет такого процесса: ${1:-}" ;; esac
    echo "$(date '+%d.%m %H:%M') запуск" >>"$POOL_DIR/$1.log"
    bash "$SELF" "$1" >>"$POOL_DIR/$1.log" 2>&1 </dev/null || rc=$?
    printf '%s\n' "$rc" >"$POOL_DIR/$1.exit"
}

cmd_start() {
    need_owner
    is_mine "$POOL_DIR/dispatcher" || die "start — окну-диспетчеру: сначала ops/pool.sh dispatcher"
    local name waited failed=0
    for name in merge-worker spawner; do
        if bg_alive "$name"; then echo "pool: ${name} уже работает"; continue; fi
        bg_start "$name"
        waited=0
        until bg_alive "$name" || [ -f "$POOL_DIR/$name.exit" ] || [ "$waited" -ge "$START_WAIT_SEC" ]; do
            sleep 1
            waited=$((waited + 1))
        done
        if bg_alive "$name"; then
            echo "pool: ${name} работает отцепленно — лог ${POOL_DIR}/${name}.log"
        else
            failed=1
            echo "pool: ${name} не поднялся (код $(cat "$POOL_DIR/$name.exit" 2>/dev/null || echo '?')) — хвост лога:" >&2
            tail -n 5 "$POOL_DIR/$name.log" >&2 2>/dev/null || true
        fi
    done
    return "$failed"
}

mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; } # Linux | macOS

# Сколько минут очередь не пуста, а воркера нет: от позднего из «воркер жил последний раз»
# и «встала самая ранняя запись». 1 — воркер жив или очередь пуста.
no_worker_min() {
    local f t since=""
    worker_alive && return 1
    for f in "$MERGE_QUEUE"/*-*; do
        [ -f "$f" ] || continue
        t="$(mtime "$f")"
        if [ -z "$since" ] || [ "$t" -lt "$since" ]; then since="$t"; fi
    done
    [ -n "$since" ] || return 1
    t="$(mtime "$WORKER_BEAT" || echo 0)"
    if [ "${t:-0}" -gt "$since" ]; then since="$t"; fi
    echo $((($(now) - since) / 60))
}

worker_alert() { # работа, что нужно от основателя — одна запись журнала на один простой
    [ ! -f "$ALERTED" ] || return 0
    : >"$ALERTED"
    spawn_journal "$1" "надзор пула поднять воркер слияния не может" "$2"
}

# Ход надзора — из цикла сторожа (сосед — воркер) и воркера (сосед — сторож).
supervise() { # сосед
    local name="$1" code m
    if bg_alive "$name"; then
        if [ "$name" = merge-worker ]; then rm -f "$ALERTED"; fi
        return 0
    fi
    is_mine "$POOL_DIR/dispatcher" || return 0 # диспетчер ушёл — поднимать незачем
    code="$(cat "$POOL_DIR/$name.exit" 2>/dev/null || true)"
    if [ "$name" = merge-worker ] && [ "$code" = 5 ]; then
        worker_alert "воркер слияния остановлен кодом 5: повторный approve аккаунтом ревьюера не прошёл" \
            "проверить вход аккаунта-ревьюера (docs/runbooks/review-process.md), потом окну-диспетчеру: ops/pool.sh start"
        return 0
    fi
    bg_start "$name"
    say "надзор: ${name} не работал${code:+ (код ${code})} — поднят"
    if [ "$name" = merge-worker ] && m="$(no_worker_min)" && [ "$m" -ge "$NO_WORKER_ALERT_MIN" ]; then
        worker_alert "очередь слияния не пуста, а воркера нет ${m} мин" "посмотреть ${POOL_DIR}/merge-worker.log"
    fi
}

merge_status() {
    local f names="" d="$POOL_DIR/merging" who m
    if worker_alive; then
        echo "    воркер слияния: работает — после approve ops/pool.sh enqueue <PR>"
    elif [ "$(cat "$POOL_DIR/merge-worker.exit" 2>/dev/null)" = 5 ]; then
        echo "    воркер слияния: остановлен кодом 5 — повторный approve аккаунтом ревьюера не прошёл; нужен основатель, затем ops/pool.sh start"
    else
        echo "    воркер слияния: нет — после approve окно мержит само: ops/pool.sh merge-one <PR>"
    fi
    for f in "$MERGE_QUEUE"/*-*; do
        if [ -f "$f" ]; then names="${names} #${f##*-}"; fi
    done
    if [ -n "$names" ]; then echo "    очередь слияния:${names}"; fi
    if m="$(no_worker_min)"; then
        echo "    !! очередь слияния не пуста, а воркера нет ${m} мин — окно-диспетчер: ops/pool.sh start"
    fi
    if [ -d "$d" ]; then
        who="окно $(window_of "$(lock_owner "$d")")"
        if worker_alive && [ "$(lock_owner "$d")" = "$(lock_owner "$POOL_DIR/merge-worker")" ]; then who="воркер"; fi
        echo "    слияние идёт: ${who}, $(lock_state "$d")"
    fi
}
