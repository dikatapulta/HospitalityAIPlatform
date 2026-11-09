#!/usr/bin/env bash
# Щит «окно пула — только в своей копии» (#890, слово основателя 09.11). Хук PreToolUse
# Claude Code из .claude/settings.json: его получают все сессии проекта, и безголовые окна.
#
# Окно пула (держит номер slot-N или замок диспетчера в $POOL_DIR) в основной папке
# проекта не запускает команд, кроме перехода в свою копию в .claude/worktrees/ и
# состояния пула; Edit и Write в файл основной папки ему запрещены, где бы окно ни стояло.
# Иначе ветка и файлы основной папки меняются под другими сессиями: 09.11 окно с промптом
# «ты уже в своей копии» стояло в основной папке, и шаг 2 «Пул окон»
# (`git checkout --detach origin/main`) сменил бы её ветку. Сессии без номера окна —
# собственные сессии основателя — хук не трогает.
#
# Команды не разбираются: решает папка, где окно стоит (cwd из входа хука), — разбор shell
# чёрным списком не сходится. Мимо проходит только явный адрес основной папки внутри команды,
# отданной из копии (`git -C …`, `cd … && …`), — это умысел, а не случайность, от которой
# щит; следующая команда такого окна начнётся уже в основной папке и получит отказ.
#
# Отказ — permissionDecision "deny" с подсказкой: окно читает причину и переходит в копию
# само. Любая ошибка самого хука — пропуск (выход 0 без вывода): щит не должен останавливать
# работу. Тесты — tests/test_pool_copy_guard.py (окно там — POOL_OWNER_PID, основная
# папка — POOL_MAIN_DIR).

set -uo pipefail # без -e: упавший хук не должен ронять ход окна

POOL_DIR="${POOL_DIR:-/tmp/necturn-windows}"
OWNER_COMM="${POOL_OWNER_COMM:-claude}"
INPUT="$(cat)"

field() { jq -r "$1 // empty" <<<"$INPUT" 2>/dev/null; }

# Процесс окна — ближайший предок `claude`: та же логика, что find_owner в ops/pool.sh.
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

# Окно пула — держит номер окна или замок диспетчера (ops/pool.sh slot, dispatcher).
pool_window() {
    local f
    for f in "$POOL_DIR"/slot-*/owner "$POOL_DIR/dispatcher/owner"; do
        if [ -f "$f" ] && [ "$(cat "$f" 2>/dev/null)" = "$1" ]; then return 0; fi
    done
    return 1
}

# Настоящий путь: /tmp и /private/tmp, симлинки — к одному виду. Файла (и папок над ним)
# может ещё не быть — Write создаёт новый: берётся ближайшая существующая папка.
real_path() {
    local p="$1" rest=""
    [ -n "$p" ] || return 0 # пустой путь — не папка хука
    while [ -n "$p" ] && [ "$p" != "/" ] && [ ! -d "$p" ]; do
        rest="/${p##*/}$rest"
        p="$(dirname "$p")"
    done
    printf '%s%s' "$(cd "$p" 2>/dev/null && pwd -P)" "$rest"
}

main_dir() {
    if [ -n "${POOL_MAIN_DIR:-}" ]; then real_path "$POOL_MAIN_DIR"; return; fi
    local common
    common="$(git -C "$(dirname "$0")" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" || return 1
    case "$common" in */.git) real_path "${common%/.git}" ;; *) return 1 ;; esac
}

in_main() { # путь — в основной папке, но не в копии
    case "$1/" in
    "$COPIES/"*) return 1 ;;
    "$MAIN/"*) return 0 ;;
    esac
    return 1
}

# Что окну можно в основной папке — одной простой командой: уйти в копию и узнать
# состояние пула. Цепочки (; && | > $(…)) — нет: всё после перехода шло бы в основной папке.
allowed_in_main() {
    local cmd="$1" target
    case "$cmd" in *[\;\&\|\<\>\`]* | *'$('* | *$'\n'*) return 1 ;; esac
    case "$cmd" in
    pwd | "make pool" | "make queue" | "make -s pool" | "make -s queue") return 0 ;;
    "git fetch" | "git fetch "*) return 0 ;;
    "git worktree add "* | "git worktree list"* | "git worktree lock "*) return 0 ;;
    ops/pool.sh* | ./ops/pool.sh* | "bash ops/pool.sh"* | "$MAIN/ops/pool.sh"*) return 0 ;;
    "cd "*)
        target="${cmd#cd }"
        target="${target#[\"\']}"
        target="${target%[\"\']}"
        case "$target" in /*) ;; *) target="$CWD/$target" ;; esac
        case "$(real_path "$target")/" in "$COPIES/"?*) return 0 ;; esac
        ;;
    esac
    return 1
}

deny() {
    jq -n --arg r "$1" \
        '{hookSpecificOutput: {hookEventName: "PreToolUse", permissionDecision: "deny", permissionDecisionReason: $r}}'
    exit 0
}

GO_COPY="Перейди в свою копию: инструмент EnterWorktree без имени; нет его — отдельными командами \`git worktree add --detach .claude/worktrees/<имя> origin/main\`, затем \`cd .claude/worktrees/<имя>\`. В основной папке окну пула доступны только (одной простой командой): cd в копию, ops/pool.sh, make pool, make queue, git fetch, git worktree add|list|lock, pwd. Читать файлы (Read, Grep) можно."

OWNER="$(find_owner)" || exit 0
pool_window "$OWNER" || exit 0
MAIN="$(main_dir)" || exit 0
[ -n "$MAIN" ] || exit 0
COPIES="$MAIN/.claude/worktrees"
CWD="$(real_path "$(field .cwd)")"

case "$(field .tool_name)" in
Bash)
    in_main "$CWD" || exit 0
    allowed_in_main "$(field .tool_input.command)" && exit 0
    deny "Окно пула стоит в основной папке проекта ($MAIN), а работает только в своей копии в .claude/worktrees/ — иначе ветка и файлы основной папки меняются под другими сессиями (#890). $GO_COPY"
    ;;
Edit | Write | MultiEdit | NotebookEdit)
    file="$(field '.tool_input.file_path // .tool_input.notebook_path')"
    [ -n "$file" ] || exit 0
    case "$file" in /*) ;; *) file="$CWD/$file" ;; esac
    file="$(real_path "$file")"
    in_main "$file" || exit 0
    case "$CWD/" in
    "$COPIES/"?*)
        copy="${CWD#"$COPIES/"}"
        copy="$COPIES/${copy%%/*}"
        deny "Файл $file — в основной папке проекта, а окно пула правит файлы только в своей копии (#890). Тот же файл в твоей копии: $copy/${file#"$MAIN/"}."
        ;;
    esac
    deny "Файл $file — в основной папке проекта, а окно пула правит файлы только в своей копии в .claude/worktrees/ (#890). $GO_COPY"
    ;;
esac
exit 0
