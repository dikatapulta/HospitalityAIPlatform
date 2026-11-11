#!/usr/bin/env bash
# Агент забора копии вне сервера (issue #1061): раз в час забирает свежайший ночной
# дамп гостевой копии на машину основателя тем же ops/backup/fetch.sh, что и
# `make backup-fetch-pilot`. Руками раз в неделю — пропущенная неделя, и единственная
# копия данных отеля живёт на одном сервере; агент держит копию вне его не старше
# суток, пока ноутбук включён хоть час в день. Не дошёл — молчит не он, а сервер:
# метку забора сторожит алертер (ERR-OPS-023).
#
#   fetch-agent.sh install <каталог>  — разложить скрипты, поставить или обновить агента
#                                       и дождаться его первого прогона;
#   fetch-agent.sh plist <каталог>    — напечатать plist агента (его пишет install).
#
# Ставит его `make backup-fetch-pilot` в конце каждого удачного прогона — как агента
# срока (local-retention.sh), забыть нельзя. Адрес сервера, ключ SSH, ключ age и
# путь к age агент получает переменными plist: install берёт их из окружения, которое
# ему передаёт fetch.sh, — без шелла и без .env репозитория.
#
# launchd-процессу macOS не даёт читать ~/Desktop, где лежит репозиторий, поэтому агент
# зовёт не репозиторий, а копию трёх скриптов в $FETCH_AGENT_HOME (по умолчанию
# ~/.necturn/backup-agent); копию обновляет каждый install.
set -euo pipefail

AGENT_LABEL="com.necturn.pilot-backup-fetch"
AGENT_HOME="${FETCH_AGENT_HOME:-$HOME/.necturn/backup-agent}"
# Минута часа: не та же, что у агента срока (минута 7), — удаление и забор не разом.
AGENT_MINUTE=37
# Сколько ждать первого прогона: забор дампа пилота по SSH — секунды, не минуты.
FIRST_RUN_WAIT_SECONDS=120

usage() {
    echo "Использование: $0 install|plist <каталог>" >&2
    exit 2
}

# sed, а не ${v//&/&amp;}: в bash 5.2 «&» в замене — найденный текст.
xml() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

env_entry() {
    echo "    <key>$1</key><string>$(xml "$2")</string>"
}

print_plist() {
    local dir="$1"
    echo '<?xml version="1.0" encoding="UTF-8"?>'
    echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
    echo '<plist version="1.0"><dict>'
    echo "  <key>Label</key><string>$AGENT_LABEL</string>"
    echo '  <key>ProgramArguments</key><array>'
    echo '    <string>/bin/bash</string>'
    echo "    <string>$(xml "$AGENT_HOME/ops/backup/fetch.sh")</string>"
    echo '    <string>pilot</string>'
    echo '    <string>--agent</string>'
    echo '  </array>'
    echo '  <key>EnvironmentVariables</key><dict>'
    env_entry PATH /usr/bin:/bin:/usr/sbin:/sbin
    env_entry PILOT_BACKUP_LOCAL_DIR "$dir"
    env_entry STAGING_HOST "$STAGING_HOST"
    env_entry STAGING_SSH_USER "${STAGING_SSH_USER:-deploy}"
    # Пустой ключ = ключи ssh-агента; путь с ~ раскрывается здесь — в plist шелла нет.
    if [ -n "${STAGING_SSH_KEY:-}" ]; then
        env_entry STAGING_SSH_KEY "${STAGING_SSH_KEY/#\~/$HOME}"
    fi
    env_entry BACKUP_AGE_KEY "${BACKUP_AGE_KEY/#\~/$HOME}"
    env_entry AGE_BIN "$AGE_BIN"
    echo '  </dict>'
    # Каждый час и при входе в систему: заснувший ноутбук launchd догоняет при
    # пробуждении, а дамп нужен один раз в сутки — лишние прогоны только проверяют.
    echo "  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>$AGENT_MINUTE</integer></dict>"
    echo '  <key>RunAtLoad</key><true/>'
    echo "  <key>StandardOutPath</key><string>$(xml "$dir/fetch-agent.log")</string>"
    echo "  <key>StandardErrorPath</key><string>$(xml "$dir/fetch-agent.log")</string>"
    echo '</dict></plist>'
}

require_inputs() {
    local name
    for name in STAGING_HOST BACKUP_AGE_KEY AGE_BIN; do
        if [ -z "${!name:-}" ]; then
            echo "ОШИБКА: не задан ${name} — агента ставит make backup-fetch-pilot, он передаёт" >&2
            echo "адрес сервера и ключи сам (docs/runbooks/pilot-copy.md, «Копия вне сервера»)." >&2
            exit 2
        fi
    done
    case "$AGE_BIN" in
        /*) ;;
        *) echo "ОШИБКА: AGE_BIN — только абсолютный путь: у агента PATH без Homebrew." >&2; exit 2 ;;
    esac
}

# Итог первого прогона (RunAtLoad): ошибка видна сейчас, пока человек у терминала,
# а не через 48 часов алертом. Не дождались — не ошибка: сказать, где смотреть.
wait_first_run() {
    local dir="$1" info code deadline=$((SECONDS + FIRST_RUN_WAIT_SECONDS))
    echo "==> Жду первый прогон агента (до ${FIRST_RUN_WAIT_SECONDS} с)..."
    while [ "$SECONDS" -lt "$deadline" ]; do
        sleep 3
        info="$(launchctl print "gui/$(id -u)/$AGENT_LABEL" 2>/dev/null || true)"
        case "$info" in *"state = running"*) continue ;; esac
        code="$(printf '%s\n' "$info" \
            | sed -nE 's/^[[:space:]]*last exit (code|status) = ([0-9]+).*/\2/p' | head -n 1)"
        [ -n "$code" ] || continue
        if [ "$code" = 0 ]; then
            echo "==> OK: агент забора $AGENT_LABEL работает (каждый час, лог ${dir}/fetch-agent.log)."
            return
        fi
        echo "ОШИБКА: первый прогон агента забора закончился кодом ${code}. Хвост лога:" >&2
        tail -n 15 "$dir/fetch-agent.log" >&2 || true
        echo "Разбор — docs/runbooks/pilot-copy.md, «Копия вне сервера»." >&2
        exit 1
    done
    echo "==> Агент поставлен, но итога первого прогона за ${FIRST_RUN_WAIT_SECONDS} с нет —"
    echo "    посмотреть позже: tail ${dir}/fetch-agent.log (последняя строка — «OK: метка…»)."
}

[ "$#" -eq 2 ] || usage
cmd="$1"
dir="$2"
case "$dir" in
    /*) ;;
    *) echo "ОШИБКА: каталог должен быть абсолютным путём: $dir" >&2; exit 2 ;;
esac

case "$cmd" in
    plist)
        require_inputs
        print_plist "$dir"
        ;;
    install)
        if [ "$(uname -s)" != "Darwin" ]; then
            echo "ОШИБКА: агент забора — только для macOS (launchd)." >&2
            exit 1
        fi
        require_inputs
        cd "$(dirname "$0")/../.."
        mkdir -p "$AGENT_HOME/ops/backup"
        chmod 700 "$AGENT_HOME"
        cp ops/backup/fetch.sh ops/backup/local-retention.sh "$AGENT_HOME/ops/backup/"
        cp ops/staging-ssh-lib.sh "$AGENT_HOME/ops/"
        plist="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
        mkdir -p "$(dirname "$plist")"
        print_plist "$dir" > "$plist"
        plutil -lint "$plist" >/dev/null
        launchctl bootout "gui/$(id -u)/$AGENT_LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$plist"
        wait_first_run "$dir"
        ;;
    *) usage ;;
esac
