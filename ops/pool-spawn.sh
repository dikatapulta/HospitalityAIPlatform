# shellcheck shell=bash
# Сторож безголовых окон пула (#588, решение основателя 31.10): без модели, по образцу
# воркера слияния. Пока живо окно-диспетчер, сам запускает рабочие окна — `claude -p`,
# каждое в своей копии (-w); оборвавшееся поднимает `--resume`, паузу держит флагом.
# Подключается из ops/pool.sh — замки, владелец и снимок `wait work` там. Шаги окон —
# docs/QUEUE.md, «Пул окон»; разбор — docs/DISCUSSION_LOG.md. Пауза, предел и состояние —
# ops/pool-spawn-ctl.sh. Тесты — tests/test_pool_spawn.py.
#
# Окно — папка workers/<id>: kind (opus|fable), session (uuid), started, tries, logpos;
# wrap и pid (обёртка и сам claude) и exit пишет обёртка _run; метки idle («брать
# нечего»), stop (остановлено паузой) и want (ждёт возобновления, внутри причина).
# Лог — workers/<id>.log, все запуски окна подряд. Обёртка отцеплена от сторожа (setsid):
# закрытое окно-диспетчер останавливает новые запуски, а работающие окна дорабатывают.

WORKERS="$POOL_DIR/workers"
PAUSE_FLAG="$POOL_DIR/pause"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
# Разрешения окон (#588, PR 2): правила классификатору и хуки — только окнам сторожа.
# Файлы — из копии сторожа, не окна: своя копия окна их не подменит.
WORKER_SETTINGS="${SELF%/*}/pool-worker-settings.json"
PERM_HOOK="${SELF%/*}/pool-perm-hook.sh"
CLAUDE_BIN="${POOL_CLAUDE_BIN:-$(command -v claude || echo "$HOME/.local/bin/claude")}"
WORKER_MODEL="${POOL_WORKER_MODEL:-opus}"
FABLE_MODEL="${POOL_FABLE_MODEL:-fable}"
WORKER_EFFORT="${POOL_WORKER_EFFORT:-xhigh}"
SPAWN_INTERVAL_SEC="${POOL_SPAWN_INTERVAL_SEC:-60}"
SPAWN_TICKS="${POOL_SPAWN_TICKS:-}" # пусто — сторож работает, пока живо окно-диспетчер
RESUME_MAX="${POOL_RESUME_MAX:-3}"
FABLE_EVERY_SEC="${POOL_FABLE_EVERY_SEC:-3600}"
LIMIT_HOLD_SEC="${POOL_LIMIT_HOLD_SEC:-3600}"
IDLE_MAX_SEC="${POOL_IDLE_MAX_SEC:-28800}"
SHORT_RUN_SEC="${POOL_SHORT_RUN_SEC:-300}"
STOP_GRACE_SEC="${POOL_STOP_GRACE_SEC:-15}"
MIN_WINDOWS="${POOL_MIN_WINDOWS:-4}" # до стольких окон память «warn» запуск не держит (docs/QUEUE.md)
ROOT=""
SNAP=""
SAID=""

# POOL_WORKER_PROMPT — только для пробы запуска (свой промпт вместо рабочего).
NEW_PROMPT="${POOL_WORKER_PROMPT:-Ты — безголовое окно пула: тебя запустил сторож (ops/pool.sh spawner) через claude -p, человека у окна нет. Ты уже в своей копии — EnterWorktree не вызывай. Выполни git fetch origin и работай по разделу «Пул окон» в docs/QUEUE.md как рабочее окно, с поправками пункта «Безголовое окно» там же.}"

resume_prompt() {
    echo "Сторож пула возобновил тебя: прошлый запуск прервался (${1}). Продолжай по пункту «Безголовое окно» раздела «Пул окон» в docs/QUEUE.md — абзац о возобновлённом окне."
}

now() { date +%s; }
say() { echo "$(date '+%H:%M') $*"; }
clock() { date -r "$1" "+$2" 2>/dev/null || date -d "@$1" "+$2" 2>/dev/null || echo '?'; } # macOS | Linux

say_once() { if [ "$SAID" != "$1" ]; then say "$1"; fi; SAID="$1"; } # причина простоя — один раз

rec_get() { cat "$WORKERS/$1/$2" 2>/dev/null || true; }
rec_put() { # id поле значение — переименованием: читающий не увидит полупустой файл
    printf '%s\n' "$3" >"$WORKERS/$1/.$2.$$"
    mv "$WORKERS/$1/.$2.$$" "$WORKERS/$1/$2"
}

worker_ids() { local d; for d in "$WORKERS"/*/; do if [ -d "$d" ]; then basename "$d"; fi; done; }

alive() { case "$1" in '' | *[!0-9]*) return 1 ;; esac; kill -0 "$1" 2>/dev/null; }

# running — окно работает (или только что запущено: обёртка ещё не записала pid);
# want — ждёт возобновления; ended — закончилось, сторож его ещё не разобрал.
worker_state() {
    local r="$WORKERS/$1" started
    if [ -f "$r/want" ]; then echo want; return 0; fi
    if [ -f "$r/exit" ]; then echo ended; return 0; fi
    if alive "$(rec_get "$1" wrap)" || alive "$(rec_get "$1" pid)"; then echo running; return 0; fi
    started="$(rec_get "$1" started)"
    if [ ! -f "$r/wrap" ] && [ $(($(now) - ${started:-0})) -lt 60 ]; then echo running; return 0; fi
    echo ended
}

ids_in_state() { local id; for id in $(worker_ids); do if [ "$(worker_state "$id")" = "$1" ]; then echo "$id"; fi; done; }

worker_of() { # id безголового окна, чей claude — процесс $1
    local id
    for id in $(worker_ids); do
        if [ "$(rec_get "$id" pid)" = "$1" ]; then echo "$id"; return 0; fi
    done
    return 1
}

# Занято номеров: живые владельцы номеров плюс запущенные окна, ещё не взявшие номер, —
# иначе за минуту до своего `slot` окно не считалось бы и сторож запустил бы лишнее.
busy_count() {
    local n=1 d owners=" " count=0 id
    while [ "$n" -le "$POOL_SLOTS" ]; do
        d="$POOL_DIR/slot-$n"
        if [ -d "$d" ] && ! lock_stale "$d"; then
            owners="${owners}$(lock_owner "$d") "
            count=$((count + 1))
        fi
        n=$((n + 1))
    done
    for id in $(ids_in_state running); do
        case "$owners" in *" $(rec_get "$id" pid) "*) ;; *) count=$((count + 1)) ;; esac
    done
    echo "$count"
}

memory_level() { sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null || echo 0; } # 1 normal, 2 warn, 4 critical

# Можно ли запустить ещё окно при занятых $1 номерах. Решение основателя 31.10: до
# MIN_WINDOWS окон — и при «warn»; сверх — только при «normal»; при «critical» — никогда:
# macOS уже закрывает программы, и новое окно убило бы работающие.
memory_allows() {
    local level
    level="$(memory_level)"
    [ "$level" != 4 ] || return 1
    [ "$level" = 1 ] || [ "$1" -lt "$MIN_WINDOWS" ]
}

cap() { # предел окон: слово основателя «запускай не больше N» (ops/pool.sh windows N), иначе POOL_SLOTS
    local c
    c="$(cat "$WORKERS/.cap" 2>/dev/null || echo "$POOL_SLOTS")"
    case "$c" in '' | *[!0-9]*) c="$POOL_SLOTS" ;; esac
    if [ "$c" -gt "$POOL_SLOTS" ]; then c="$POOL_SLOTS"; fi
    echo "$c"
}

held_till() { cat "$WORKERS/.hold" 2>/dev/null || echo 0; }
held() { [ "$(now)" -lt "$(held_till)" ]; }
hold() { printf '%s\n' "$(($(now) + LIMIT_HOLD_SEC))" >"$WORKERS/.hold"; }

# 0 — с тех пор как окно kind ушло со словами «брать нечего», в пуле ничего не менялось
# (снимок `wait work` тот же и ему меньше 8 часов): новое окно kind не нужно.
quiet_since_idle() {
    local f="$WORKERS/.idle-$1"
    [ -f "$f" ] || return 1
    if [ -z "$(find "$f" -mmin +$((IDLE_MAX_SEC / 60)) 2>/dev/null)" ] && [ "$SNAP" = "$(cat "$f")" ]; then
        return 0
    fi
    rm -f "$f"
    return 1
}

fable_due() { # окно Fable — одно, и новое не чаще раза в час
    local id last
    for id in $(worker_ids); do
        if [ "$(rec_get "$id" kind)" = fable ]; then return 1; fi
    done
    last="$(cat "$WORKERS/.fable-last" 2>/dev/null || echo 0)"
    [ $(($(now) - last)) -ge "$FABLE_EVERY_SEC" ] || return 1
    ! quiet_since_idle fable
}

limit_hit() { # в выводе последнего запуска — слова лимита подписки (docs: errors, «Usage limits»)
    local log="$WORKERS/$1.log" pos tail_text
    pos="$(rec_get "$1" logpos)"
    tail_text="$(tail -c +$((${pos:-0} + 1)) "$log" 2>/dev/null || true)"
    grep -qiE 'hit your [a-z ]*limit|usage limit' <<<"$tail_text"
}

spawn_journal() { # работа, сделано, от основателя — запись сторожа в журнал основателя
    local file
    file="$(journal_path)" || return 0
    printf '%s\n' "$(date '+%d.%m %H:%M') · Сторож · ${1}" "Сделано: ${2}" "От тебя: ${3}" "" >>"$file"
}

# Окно закончилось — разобрать: остановлено паузой, ушло, упёрлось в лимит или оборвалось.
reap() {
    local id="$1" r="$WORKERS/$1" code kind started tries
    code="$(rec_get "$id" exit)" kind="$(rec_get "$id" kind)" started="$(rec_get "$id" started)"
    rm -f "$r/exit" "$r/pid" "$r/wrap"
    # Остановленное паузой продолжается всегда: после SIGINT claude может выйти и с кодом 0.
    if [ -f "$r/stop" ]; then
        rm -f "$r/stop"
        rec_put "$id" want "пауза"
        say "окно ${id}: остановлено паузой — продолжит после ops/pool.sh resume"
        return 0
    fi
    if [ "$code" = 0 ]; then
        # Короткий запуск без метки — тоже «брать нечего»: иначе окно, забывшее idle,
        # перезапускалось бы каждую минуту. Снимок не снялся — метки нет, окно запустится.
        if [ -f "$r/idle" ] || [ $(($(now) - ${started:-0})) -lt "$SHORT_RUN_SEC" ]; then
            snapshot work >"$WORKERS/.idle-$kind" 2>/dev/null || rm -f "$WORKERS/.idle-$kind"
        fi
        rm -rf "$r"
        say "окно ${id}: закончило"
        return 0
    fi
    if limit_hit "$id"; then
        hold
        rec_put "$id" want "лимит подписки"
        say "окно ${id}: лимит подписки — сторож стоит до $(clock "$(held_till)" '%H:%M'), потом продолжит это окно"
        return 0
    fi
    tries=$(($(rec_get "$id" tries) + 1))
    if [ "$tries" -gt "$RESUME_MAX" ]; then
        hold
        spawn_journal "безголовое окно ${id} оборвалось $((tries - 1)) раза подряд — снято" \
            "сторож перестал его поднимать и стоит $((LIMIT_HOLD_SEC / 60)) мин, чтобы не жечь подписку на сломанном запуске" \
            "посмотреть лог ${WORKERS}/${id}.log; если окно держало PR — его продолжит следующее окно по комментарию «Передача» или заявке"
        rm -rf "$r"
        say "окно ${id}: оборвалось ${RESUME_MAX} раза подряд — снято, запись в журнале"
        return 0
    fi
    rec_put "$id" tries "$tries"
    rec_put "$id" want "обрыв, код ${code:-нет}, попытка ${tries} из ${RESUME_MAX}"
    say "окно ${id}: оборвалось (код ${code:-нет}) — возобновлю"
}

# Рабочему окну не передаются: окружение сессии диспетчера (claude внутри claude с ним
# не стартует или путает сессии), ключи API (окно работает по подписке, а не с баланса
# Console — CLAUDE.md, платные прогоны) и подмена владельца замков из тестов.
clean_env_names() {
    env | awk -F= '/^CLAUDE[A-Z0-9_]*=/ || /^ANTHROPIC_(API_KEY|AUTH_TOKEN)=/ || /^POOL_OWNER_PID=/ { print $1 }'
}

# Отцепить от сторожа: своя сессия процессов — закрытие окна-диспетчера окно не убьёт.
# SIGINT — снова по умолчанию: фоновой команде bash велел его игнорировать, а
# унаследованное «игнорировать» потомок-bash уже не снимет, и пауза не дошла бы до окна.
detach() {
    if [ -x /usr/bin/perl ]; then
        (/usr/bin/perl -MPOSIX -e '$SIG{INT} = $SIG{QUIT} = "DEFAULT"; POSIX::setsid(); exec @ARGV or die' \
            "$@" </dev/null >/dev/null 2>&1 &)
    else
        ("$@" </dev/null >/dev/null 2>&1 &)
    fi
}

launch() { # id причина-возобновления (пусто — первый запуск)
    local id="$1" why="$2" kind model session n
    local -a unset_env=(-u CLAUDECODE) extra
    kind="$(rec_get "$id" kind)" session="$(rec_get "$id" session)"
    model="$WORKER_MODEL"
    if [ "$kind" = fable ]; then model="$FABLE_MODEL"; fi
    for n in $(clean_env_names); do unset_env+=(-u "$n"); done
    if [ -z "$why" ]; then
        extra=(-w "pool-$id" --session-id "$session" --name "pool-$id" "$NEW_PROMPT")
    else
        extra=(--resume "$session" "$(resume_prompt "$why")")
    fi
    rm -f "$WORKERS/$id/want" "$WORKERS/$id/idle" "$WORKERS/$id/stop"
    rec_put "$id" started "$(now)"
    rec_put "$id" logpos "$(($(cat "$WORKERS/$id.log" 2>/dev/null | wc -c)))"
    (cd "$ROOT" && detach bash "$SELF" _run "$id" nice -n 10 env "${unset_env[@]}" POOL_WORKER="$id" \
        POOL_HOOK="$PERM_HOOK" "$CLAUDE_BIN" -p --model "$model" --effort "$WORKER_EFFORT" \
        --permission-mode auto --permission-prompts none --settings "$WORKER_SETTINGS" "${extra[@]}")
    say "окно ${id} (${kind}): ${why:+возобновлено — ${why}; }запущено, лог ${WORKERS}/${id}.log"
}

new_worker() { # kind
    local id session
    id="$(date '+%m%d-%H%M%S')"
    while [ -e "$WORKERS/$id" ] || [ -e "$WORKERS/$id.log" ]; do id="${id}x"; done # копия pool-<id> — новая
    session="$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)"
    mkdir "$WORKERS/$id"
    rec_put "$id" kind "$1"
    rec_put "$id" session "$(echo "$session" | tr 'A-Z' 'a-z')"
    rec_put "$id" tries 0
    if [ "$1" = fable ]; then now >"$WORKERS/.fable-last"; fi
    launch "$id" ""
}

# Один ход сторожа: разобрать закончившиеся окна, затем запустить одно, если можно.
# Первые строки списков — через awk, не head: head закрывает трубу раньше, и под
# pipefail присваивание падает, а с ним и сторож (set -e).
spawn_tick() {
    local id busy limit
    for id in $(ids_in_state ended); do reap "$id"; done
    if [ -f "$PAUSE_FLAG" ]; then say_once "пауза — новых окон нет"; return 0; fi
    if held; then say_once "стою до $(clock "$(held_till)" '%H:%M')"; return 0; fi
    busy="$(busy_count)" limit="$(cap)"
    if [ "$busy" -ge "$limit" ]; then say_once "окон ${busy} — предел ${limit}, новых нет"; return 0; fi
    if ! memory_allows "$busy"; then say_once "давление памяти $(memory_level) при ${busy} окнах — жду"; return 0; fi
    if ! SNAP="$(snapshot work 2>/dev/null)"; then say_once "GitHub не отвечает — жду"; return 0; fi
    id="$(ids_in_state want | awk 'NR == 1')"
    if [ -n "$id" ]; then
        launch "$id" "$(rec_get "$id" want)"
    elif fable_due; then
        new_worker fable
    elif quiet_since_idle opus; then
        say_once "брать нечего — жду изменений в пуле"
        return 0
    else
        new_worker opus
    fi
    SAID=""
}

spawner_is_me() {
    is_mine "$POOL_DIR/spawner" && [ "$(cat "$POOL_DIR/spawner/pid" 2>/dev/null)" = "$$" ] && owner_alive "$OWNER"
}

spawner_alive() { [ -d "$POOL_DIR/spawner" ] && ! lock_stale "$POOL_DIR/spawner"; }
spawner_cleanup() { if spawner_is_me; then rm -rf "$POOL_DIR/spawner"; fi; }

main_root() { # главная папка репозитория: из неё -w кладёт копии в .claude/worktrees/
    local common
    common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
    dirname "$common"
}

cmd_spawner() {
    need_owner
    is_mine "$POOL_DIR/dispatcher" || die "сторож запускает окно-диспетчер: сначала ops/pool.sh dispatcher"
    [ -x "$CLAUDE_BIN" ] || die "не нашёл claude (${CLAUDE_BIN}) — задай POOL_CLAUDE_BIN"
    [ -f "$WORKER_SETTINGS" ] && [ -f "$PERM_HOOK" ] || die "нет ${WORKER_SETTINGS} или ${PERM_HOOK} — сторож без них не запускает окна"
    ROOT="${POOL_SPAWN_ROOT:-$(main_root)}" || die "не нашёл корень репозитория"
    if ! acquire spawner; then
        echo "pool: сторож уже работает" >&2
        exit 1
    fi
    printf '%s\n' "$$" >"$POOL_DIR/spawner/.pid.$$"
    mv "$POOL_DIR/spawner/.pid.$$" "$POOL_DIR/spawner/pid"
    trap spawner_cleanup EXIT
    mkdir -p "$WORKERS"
    echo "pool: сторож работает — окон не больше $(cap), при памяти warn — до ${MIN_WINDOWS}; ${WORKER_MODEL} ${WORKER_EFFORT}, логи ${WORKERS}/"
    local ticks=0
    while spawner_is_me; do
        spawn_tick
        ticks=$((ticks + 1))
        if [ -n "$SPAWN_TICKS" ] && [ "$ticks" -ge "$SPAWN_TICKS" ]; then return 0; fi
        sleep "$SPAWN_INTERVAL_SEC"
    done
    echo "pool: сторож остановлен — его замок снят (release или закрыто окно-диспетчер); работающие окна дорабатывают"
}

# Внутренняя: обёртка одного запуска окна. set -m — окно в своей группе процессов
# и слышит SIGINT (фоновой команде без него bash велит SIGINT игнорировать).
cmd_spawn_run() {
    local id="$1" rc=0
    shift
    [ -d "$WORKERS/$id" ] || die "нет окна ${id}"
    rec_put "$id" wrap "$$"
    set -m
    "$@" >>"$WORKERS/$id.log" 2>&1 </dev/null &
    rec_put "$id" pid "$!"
    wait "$!" || rc=$?
    rec_put "$id" exit "$rc"
}
