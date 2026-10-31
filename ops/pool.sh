#!/usr/bin/env bash
# Пул окон (#503, правило исполнения 10): номер окна, турникет заявки, заявки на ревью,
# доработку и merge, ожидание работы без трат, журнал основателя; конвейер слияния —
# ops/pool-merge.sh, «занять номер» — ops/pool-reserve.sh, сторож безголовых окон —
# ops/pool-spawn.sh (подключаются ниже). Шаги окна
# и окна-диспетчера — docs/QUEUE.md, раздел «Пул окон».
#
# Замок — папка в $POOL_DIR: mkdir либо создаёт её, либо отказывает, поэтому два окна
# не займут одно. Владелец замка — процесс сессии `claude`, ближайший предок скрипта
# (файл owner в папке). Закрытое окно отпускает свои замки само: следующий, кому замок
# нужен, видит мёртвого владельца и снимает его. Замок без файла owner (окна, начатые
# промптом до этого скрипта) снимается только вручную — `free` или перезагрузкой Mac.
#
# Коды выхода: 0 — сделано; 1 — занято другим окном; 2 — ошибка вызова;
# 3 — все номера окон заняты; 4 — турникет или замок слияния не освободился за отведённое
# время; 5 — воркер или merge-one остановлен: повторный approve аккаунтом ревьюера не прошёл.
# Тесты — tests/test_pool_script.py: окно там подменяется процессом `sleep`
# (POOL_OWNER_PID и POOL_OWNER_COMM).

set -euo pipefail

POOL_DIR="${POOL_DIR:-/tmp/necturn-windows}"
POOL_SLOTS="${POOL_SLOTS:-5}"
OWNER_COMM="${POOL_OWNER_COMM:-claude}"
TURNSTILE_MAX_MIN="${POOL_TURNSTILE_MAX_MIN:-30}"
ENTER_TIMEOUT_SEC="${POOL_ENTER_TIMEOUT_SEC:-540}"
OWNER=""

die() { echo "pool: $*" >&2; exit 2; }

usage() {
    cat <<'EOF'
ops/pool.sh — пул окон (docs/QUEUE.md, «Пул окон»)
  status                      состояние пула (то же, что make pool)
  slot                        взять номер окна (печатает N); 3 — все заняты
  enter | leave               войти в турникет (ждёт до 9 минут; 4 — не дождался) / выйти
  claim review-<PR>           заявка внутри турникета; также rework-<PR>, merge-<PR>
  release                     снять все свои замки (конец работы)
  reserve migration|spec|policy  занять номер миграции, спеки или версию политики гостя
  enqueue <PR>                после approve — PR в очередь слияния (1 — воркера нет: merge-one)
  merge-one <PR>              воркера нет: то же, что воркер, для одного PR, в фоне (1 — не смержен)
  merge-worker                воркер очереди слияния, в фоне; запускает окно-диспетчер
  merge-enter | merge-leave   замок слияния (merge-one берёт его сам; ждёт до 2 часов) / отпустить
  wait work                   ждать изменения PR или замков (до 8 часов)
  wait ask                    ждать вопроса окна или записи журнала (до часа) — для диспетчера
  wait answer <имя вопроса>   ждать файла ask/<имя>.answer
  journal "<работа>" "<сделано>" "<от тебя>"   запись в журнал основателя
  dispatcher                  стать окном-диспетчером (1 — диспетчер уже есть)
  spawner                     сторож безголовых окон, в фоне; запускает окно-диспетчер
  pause [--now] | resume      сторожу: новых окон нет (--now — остановить и работающие) / продолжить
  make-room [K]               пауза и остановить K окон: сначала ждущие, потом самые молодые
  idle                        безголовое окно: брать нечего (вместо wait work)
  free <замок>                снять замок вручную — только по слову основателя
EOF
}

# --- владелец замков ------------------------------------------------------------

owner_alive() {
    local pid="$1" comm
    case "$pid" in '' | *[!0-9]*) return 1 ;; esac
    comm="$(ps -o comm= -p "$pid" 2>/dev/null)" || return 1
    [ -n "$comm" ] || return 1
    [ "${comm##*/}" = "$OWNER_COMM" ]
}

find_owner() {
    if [ -n "${POOL_OWNER_PID:-}" ]; then echo "$POOL_OWNER_PID"; return 0; fi
    local pid=$$ comm
    while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
        comm="$(ps -o comm= -p "$pid" 2>/dev/null)" || return 1
        if [ -n "$comm" ] && [ "${comm##*/}" = "$OWNER_COMM" ]; then echo "$pid"; return 0; fi
        pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')"
    done
    return 1
}

need_owner() {
    OWNER="$(find_owner)" || die "не нашёл процесс окна Claude среди предков — запускай из окна Claude"
    mkdir -p "$POOL_DIR/ask"
}

# --- замки ---------------------------------------------------------------------

lock_owner() { cat "$1/owner" 2>/dev/null || true; }

is_mine() { [ -f "$1/owner" ] && [ "$(lock_owner "$1")" = "$OWNER" ]; }

# Брошен: владелец мёртв; воркер слияния — ещё и если умер его процесс (файл pid);
# турникет и замок слияния — ещё и если держатся дольше своего предела (окно внутри
# ждёт человека — остальных это держать не должно).
# Замок без владельца не трогаем — кроме диспетчера: «занят» безопасен для номера
# и заявки, а живым считать неизвестного диспетчера нельзя — окна ждали бы ответа,
# который некому передать.
lock_max_min() {
    case "$1" in
    turnstile) echo "$TURNSTILE_MAX_MIN" ;;
    merging) echo "$MERGING_MAX_MIN" ;;
    esac
}

lock_stale() {
    local d="$1" max
    if [ ! -f "$d/owner" ]; then
        [ "${d##*/}" = dispatcher ]
        return
    fi
    owner_alive "$(lock_owner "$d")" || return 0
    if [ -f "$d/pid" ] && ! kill -0 "$(cat "$d/pid" 2>/dev/null)" 2>/dev/null; then return 0; fi
    max="$(lock_max_min "${d##*/}")"
    if [ -n "$max" ] && [ -n "$(find "$d" -maxdepth 0 -mmin +"$max" 2>/dev/null)" ]; then
        return 0
    fi
    return 1
}

# owner пишется переименованием: читающий никогда не увидит пустой файл и не примет
# только что взятый замок за брошенный.
write_owner() {
    printf '%s\n' "$OWNER" >"$1/.owner.$$"
    date '+%Y-%m-%d %H:%M' >"$1/since"
    mv "$1/.owner.$$" "$1/owner"
}

# Снимать брошенный замок может только одно окно за раз (папка .break); после снятия
# кто первым сделает mkdir, тот и взял.
break_if_stale() {
    local d="$1" guard="$1.break"
    lock_stale "$d" || return 1
    if ! mkdir "$guard" 2>/dev/null; then
        if [ -n "$(find "$guard" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
            rmdir "$guard" 2>/dev/null || true
        fi
        return 1
    fi
    if lock_stale "$d"; then rm -rf "$d"; fi
    rmdir "$guard"
}

acquire() {
    local d="$POOL_DIR/$1"
    if mkdir "$d" 2>/dev/null; then
        write_owner "$d"
        return 0
    fi
    break_if_stale "$d" || return 1
    mkdir "$d" 2>/dev/null || return 1
    write_owner "$d"
}

my_slot() {
    local n=1
    while [ "$n" -le "$POOL_SLOTS" ]; do
        if is_mine "$POOL_DIR/slot-$n"; then echo "$n"; return 0; fi
        n=$((n + 1))
    done
    return 1
}

window_of() {
    local n=1
    if [ -n "$1" ]; then
        while [ "$n" -le "$POOL_SLOTS" ]; do
            if [ "$(lock_owner "$POOL_DIR/slot-$n")" = "$1" ]; then echo "$n"; return 0; fi
            n=$((n + 1))
        done
    fi
    echo "?"
}

dispatcher_alive() { [ -d "$POOL_DIR/dispatcher" ] && ! lock_stale "$POOL_DIR/dispatcher"; }

valid_claim() {
    case "$1" in review-* | rework-* | merge-*) ;; *) return 1 ;; esac
    case "${1#*-}" in '' | *[!0-9]*) return 1 ;; esac
}

lock_state() {
    local d="$1"
    if [ ! -f "$d/owner" ]; then
        echo "занято, владелец неизвестен (окно со старым промптом; снять — только по слову основателя)"
    elif owner_alive "$(lock_owner "$d")"; then
        echo "занято с $(cat "$d/since" 2>/dev/null || echo '?')"
    else
        echo "брошено (окно закрыто) — снимется при следующем взятии"
    fi
}

claim_dirs() {
    local d
    for d in "$POOL_DIR"/review-* "$POOL_DIR"/rework-* "$POOL_DIR"/merge-*; do
        [ -d "$d" ] || continue
        case "$d" in *.break) continue ;; esac
        echo "$d"
    done
}

# --- команды -------------------------------------------------------------------

cmd_slot() {
    need_owner
    local n
    if n="$(my_slot)"; then echo "$n"; return 0; fi
    n=1
    while [ "$n" -le "$POOL_SLOTS" ]; do
        if acquire "slot-$n"; then echo "$n"; return 0; fi
        n=$((n + 1))
    done
    echo "pool: заняты все номера окон (${POOL_SLOTS}) — жди (ops/pool.sh wait work) и пробуй снова" >&2
    exit 3
}

cmd_enter() {
    need_owner
    my_slot >/dev/null || die "сначала номер окна: ops/pool.sh slot"
    if is_mine "$POOL_DIR/turnstile"; then echo "pool: ты уже в турникете"; return 0; fi
    local waited=0
    until acquire turnstile; do
        if [ "$waited" -ge "$ENTER_TIMEOUT_SEC" ]; then
            echo "pool: турникет занят уже ${waited} с — повтори ops/pool.sh enter" >&2
            exit 4
        fi
        sleep 3
        waited=$((waited + 3))
    done
    echo "pool: ты в турникете — только make queue, выбор и заявка; потом ops/pool.sh leave"
}

cmd_leave() {
    need_owner
    if is_mine "$POOL_DIR/turnstile"; then
        rm -rf "$POOL_DIR/turnstile"
        echo "pool: турникет свободен"
        return 0
    fi
    echo "pool: турникет не твой (снят как зависший?) — проверь свою заявку в make pool" >&2
    exit 1
}

cmd_claim() {
    [ $# -eq 1 ] || die "использование: ops/pool.sh claim review-<PR> | rework-<PR> | merge-<PR>"
    valid_claim "$1" || die "заявка — review-<PR>, rework-<PR> или merge-<PR>, а не «${1}»"
    need_owner
    is_mine "$POOL_DIR/turnstile" || die "заявка — только внутри турникета: ops/pool.sh enter"
    if is_mine "$POOL_DIR/$1"; then echo "pool: ${1} уже твоя"; return 0; fi
    if acquire "$1"; then echo "pool: ${1} — твоя"; return 0; fi
    echo "pool: ${1} уже занята окном $(window_of "$(lock_owner "$POOL_DIR/$1")") — бери другую работу" >&2
    exit 1
}

cmd_release() {
    need_owner
    local d removed=""
    for d in "$POOL_DIR"/*; do
        [ -d "$d" ] || continue
        if is_mine "$d"; then
            rm -rf "$d"
            removed="${removed} $(basename "$d")"
        fi
    done
    echo "pool: снято:${removed:- ничего}"
}

cmd_dispatcher() {
    need_owner
    if is_mine "$POOL_DIR/dispatcher"; then echo "pool: ты уже диспетчер"; return 0; fi
    if acquire dispatcher; then echo "pool: ты диспетчер"; return 0; fi
    echo "pool: диспетчер уже работает — второе окно-диспетчер не нужно" >&2
    exit 1
}

cmd_free() {
    [ $# -eq 1 ] || die "использование: ops/pool.sh free slot-<N> | turnstile | dispatcher | review-<PR> | …"
    case "$1" in
    turnstile | dispatcher | merging | merge-worker | spawner) ;;
    slot-*) case "${1#slot-}" in '' | *[!0-9]*) die "нет такого замка: ${1}" ;; esac ;;
    *) valid_claim "$1" || die "нет такого замка: ${1}" ;;
    esac
    if [ -d "$POOL_DIR/$1" ]; then
        rm -rf "${POOL_DIR:?}/$1"
        echo "pool: ${1} снят"
    else
        echo "pool: ${1} и так свободен"
    fi
}

journal_path() {
    if [ -n "${POOL_JOURNAL:-}" ]; then echo "$POOL_JOURNAL"; return 0; fi
    local common
    common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
    echo "$(dirname "$common")/журнал-окон.local"
}

cmd_journal() {
    [ $# -eq 3 ] || die "использование: ops/pool.sh journal \"<работа и PR>\" \"<сделано>\" \"<что нужно от основателя или «ничего»>\""
    need_owner
    local n file
    n="$(my_slot || echo '?')"
    if worker_of "$OWNER" >/dev/null; then n="${n} (безголовое)"; fi
    file="$(journal_path)" || die "не нашёл корень репозитория для журнала"
    printf '%s\n' "$(date '+%d.%m %H:%M') · Окно ${n} · ${1}" "Сделано: ${2}" "От тебя: ${3}" "" >>"$file"
    echo "pool: записал в ${file}"
}

cmd_status() {
    echo "==> Пул окон: ${POOL_DIR}, номеров ${POOL_SLOTS}"
    if [ ! -d "$POOL_DIR" ]; then echo "    пусто — ни одно окно ещё не брало номер"; return 0; fi
    local n=1 d q
    while [ "$n" -le "$POOL_SLOTS" ]; do
        d="$POOL_DIR/slot-$n"
        if [ -d "$d" ]; then echo "    окно ${n}: $(lock_state "$d")"; else echo "    окно ${n}: свободно"; fi
        n=$((n + 1))
    done
    d="$POOL_DIR/turnstile"
    if [ -d "$d" ]; then
        echo "    турникет: окно $(window_of "$(lock_owner "$d")"), $(lock_state "$d")"
    else
        echo "    турникет: свободен"
    fi
    for d in $(claim_dirs); do
        echo "    заявка $(basename "$d"): окно $(window_of "$(lock_owner "$d")"), $(lock_state "$d")"
    done
    reserve_status
    if dispatcher_alive; then
        echo "    диспетчер: работает — вопросы основателю идут файлом в ${POOL_DIR}/ask/"
    else
        echo "    диспетчер: нет — вопросы основателю окна задают в своём чате"
    fi
    merge_status
    spawn_status
    for q in "$POOL_DIR"/ask/*.md; do
        [ -f "$q" ] || continue
        [ -f "${q%.md}.answer" ] && continue
        echo "    вопрос без ответа: $(basename "$q")"
    done
}

locks_snapshot() {
    local d name
    for d in "$POOL_DIR"/slot-* $(claim_dirs); do
        [ -d "$d" ] || continue
        name="$(basename "$d")"
        if lock_stale "$d"; then echo "${name}:брошен"; else echo "$name"; fi
    done
}

snapshot() {
    local f
    case "$1" in
    work)
        gh pr list --state open --limit 200 --json number,isDraft,reviewDecision,headRefOid \
            --jq 'sort_by(.number) | .[] | "\(.number) \(.isDraft) \(.reviewDecision) \(.headRefOid)"' ||
            return 1
        locks_snapshot
        ;;
    ask)
        for f in "$POOL_DIR"/ask/*.md; do [ -f "$f" ] && basename "$f"; done
        f="$(journal_path)" || return 1
        if [ -f "$f" ]; then wc -c <"$f"; else echo 0; fi
        ;;
    esac
}

cmd_wait() {
    local mode="${1:-}" name="${2:-}" interval max waited=0 start="" cur have=0
    case "$mode" in
    work) interval="${POOL_WAIT_INTERVAL_SEC:-300}" max="${POOL_WAIT_MAX_SEC:-28800}" ;;
    ask) interval="${POOL_WAIT_INTERVAL_SEC:-120}" max="${POOL_WAIT_MAX_SEC:-3600}" ;;
    answer)
        case "$name" in '' | *[!A-Za-z0-9._-]*) die "использование: ops/pool.sh wait answer <имя вопроса без .md>" ;; esac
        interval="${POOL_WAIT_INTERVAL_SEC:-20}" max="${POOL_WAIT_MAX_SEC:-28800}"
        ;;
    *) die "использование: ops/pool.sh wait work | ask | answer <имя>" ;;
    esac
    if [ "$mode" = answer ]; then
        echo "pool: жду ответа ${POOL_DIR}/ask/${name}.answer, проверка раз в ${interval} с"
    fi
    while :; do
        if [ "$mode" = answer ]; then
            if [ -f "$POOL_DIR/ask/${name}.answer" ]; then echo "pool: ответ пришёл — ${POOL_DIR}/ask/${name}.answer"; return 0; fi
            if ! dispatcher_alive; then echo "pool: диспетчера нет — задай вопрос основателю в своём чате"; return 0; fi
        elif cur="$(snapshot "$mode")"; then
            if [ "$have" -eq 0 ]; then
                start="$cur" have=1
                echo "pool: жду (${mode}), проверка раз в ${interval} с, не дольше ${max} с"
            elif [ "$cur" != "$start" ]; then echo "pool: изменилось — пора смотреть снова"; return 0; fi
        fi
        if [ "$waited" -ge "$max" ]; then echo "pool: время вышло (${max} с) — без изменений"; return 0; fi
        sleep "$interval"
        waited=$((waited + interval))
    done
}

# shellcheck source=ops/pool-merge.sh
. "$(dirname "$0")/pool-merge.sh"
# shellcheck source=ops/pool-reserve.sh
. "$(dirname "$0")/pool-reserve.sh"
# shellcheck source=ops/pool-spawn.sh
. "$(dirname "$0")/pool-spawn.sh"

cmd="${1:-status}"
if [ $# -gt 0 ]; then shift; fi
case "$cmd" in
status) cmd_status ;;
slot) cmd_slot ;;
enter) cmd_enter ;;
leave) cmd_leave ;;
claim) cmd_claim "$@" ;;
release) cmd_release ;;
reserve) cmd_reserve "$@" ;;
wait) cmd_wait "$@" ;;
journal) cmd_journal "$@" ;;
dispatcher) cmd_dispatcher ;;
enqueue) cmd_enqueue "$@" ;;
merge-one) cmd_merge_one "$@" ;;
merge-worker) cmd_merge_worker ;;
merge-enter) cmd_merge_enter ;;
merge-leave) cmd_merge_leave ;;
spawner | pause | resume | make-room | idle | _run) spawn_cmd "$cmd" "$@" ;;
free) cmd_free "$@" ;;
help | -h | --help) usage ;;
*) die "нет команды «${cmd}» — ops/pool.sh help" ;;
esac
