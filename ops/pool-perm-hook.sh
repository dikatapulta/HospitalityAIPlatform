#!/usr/bin/env bash
# Хуки разрешений безголовых окон сторожа (#588, PR 2). Claude Code зовёт их сам: файл
# настроек ops/pool-worker-settings.json сторож передаёт только своим окнам (--settings),
# путь к этому скрипту — в POOL_HOOK. Окна в панели и терминале хуков не получают.
#
# В режиме auto решает классификатор; его отказ — событие PermissionDenied: хук пишет
# строку в журнал разрешений и отказ не трогает. PermissionRequest приходит, только когда
# окно спросило бы человека (правило ask — платный прогон, scp; опасное rm), а человека
# у безголового окна нет: хук пишет строку и отказывает с подсказкой — вопрос основателю
# файлом (шаг 4 «Пул окон», docs/QUEUE.md). Журнал — для сводки диспетчера.
# Тесты — tests/test_pool_perm_hook.py.

set -uo pipefail # без -e: упавший хук не должен ронять ход окна

POOL_DIR="${POOL_DIR:-/tmp/necturn-windows}"
LOG="$POOL_DIR/workers/permissions.log"
WHAT_MAX=200 # длина команды в строке журнала: читать глазами, а не хранить

INPUT="$(cat)"

field() { jq -r "$1 // empty" <<<"$INPUT" 2>/dev/null | tr '\n' ' ' | sed 's/ *$//'; }

# Что окно хотело сделать — одной строкой: команда, файл, адрес или вход инструмента.
what() {
    local w
    w="$(field '.tool_input.command // .tool_input.file_path // .tool_input.url')"
    if [ -z "$w" ]; then w="$(jq -c '.tool_input // {}' <<<"$INPUT" 2>/dev/null)"; fi
    if [ "${#w}" -gt "$WHAT_MAX" ]; then w="${w:0:$WHAT_MAX}…"; fi
    printf '%s' "$w"
}

log_line() { # вид причина
    mkdir -p "${LOG%/*}"
    printf '%s · окно %s · %s · %s: %s — %s\n' "$(date '+%d.%m %H:%M')" "${POOL_WORKER:-?}" \
        "$1" "$(field .tool_name)" "$(what)" "$2" >>"$LOG"
}

REQUEST_MESSAGE="Это действие решает только основатель (правило ask — платный прогон или scp — либо опасное удаление), а у безголового окна спросить некого. Не повторяй его. Без него работу не закончить — блокирующий вопрос основателю файлом по шагу 4 раздела «Пул окон» docs/QUEUE.md (платный прогон — с планом и ценой по CLAUDE.md), затем ops/pool.sh wait answer в фоне; иначе продолжай без него."

case "${1:-}" in
denied)
    log_line "отказ классификатора" "$(field .reason)"
    ;;
request)
    log_line "к основателю" "отказано хуком, окну велено спросить файлом"
    jq -n --arg m "$REQUEST_MESSAGE" \
        '{hookSpecificOutput: {hookEventName: "PermissionRequest", decision: {behavior: "deny", message: $m}}}'
    ;;
*)
    echo "использование: ops/pool-perm-hook.sh denied | request (вход — JSON события на stdin)" >&2
    exit 2
    ;;
esac
exit 0
