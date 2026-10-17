#!/usr/bin/env bash
# Срок жизни offsite-копий бэкапов гостевой копии на машине основателя (#391).
#
# Договор с отелем (п. 4.3.4) и политика конфиденциальности (§7) обещают гостю:
# копия бэкапа вне сервера живёт не дольше 14 дней. Удалять её только при
# `make backup-fetch-pilot` мало — пропущенная неделя, и файл живёт 21 день.
# Поэтому удаляет launchd-агент каждый час, а fetch.sh ставит агента заново при
# каждом прогоне (забыть установить нельзя).
#
#   local-retention.sh prune <каталог>    — удалить файлы старше срока сейчас;
#   local-retention.sh install <каталог>  — поставить/обновить launchd-агента.
#
# Граница — 14 суток минус час, по минутам, а не `-mtime +14`: тот округляет
# сутки вниз и держит файл ~15 суток (#394); минус час — запас на шаг агента.
# Возраст считается от mtime, а fetch.sh копирует файл с `scp -p`: часы те же,
# что у retention на сервере (время записи дампа), а не время скачивания.
#
# Агент зовёт /usr/bin/find напрямую, без этого скрипта: launchd-процессу macOS
# не даёт читать ~/Desktop, где лежит репозиторий, — и каталог копий поэтому
# тоже вне Desktop (умолчание в fetch.sh).
set -euo pipefail

RETENTION_MINUTES=$((14 * 1440 - 60))
AGENT_LABEL="com.necturn.pilot-backup-retention"

# Аргументы find — один источник и для prune, и для агента.
find_args() {
    printf '%s\n' "$1" -maxdepth 1 -type f \
        '(' -name '*.dump.age' -o -name '*.dump' -o -name '*.part' ')' \
        -mmin "+$RETENTION_MINUTES" -print -delete
}

usage() {
    echo "Использование: $0 prune|install <каталог>" >&2
    exit 2
}

[ "$#" -eq 2 ] || usage
cmd="$1"
dir="$2"
case "$dir" in
    /*) ;;
    *) echo "ОШИБКА: каталог должен быть абсолютным путём: $dir" >&2; exit 2 ;;
esac

case "$cmd" in
    prune)
        [ -d "$dir" ] || exit 0
        args=()
        while IFS= read -r a; do args+=("$a"); done < <(find_args "$dir")
        find "${args[@]}"
        ;;
    install)
        if [ "$(uname -s)" != "Darwin" ]; then
            echo "ОШИБКА: агент автоудаления — только для macOS (launchd); на этой" >&2
            echo "машине срок 14 дней держать нечем — копии копии сюда не забирать." >&2
            exit 1
        fi
        plist="$HOME/Library/LaunchAgents/$AGENT_LABEL.plist"
        mkdir -p "$(dirname "$plist")"
        {
            echo '<?xml version="1.0" encoding="UTF-8"?>'
            echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
            echo '<plist version="1.0"><dict>'
            echo "  <key>Label</key><string>$AGENT_LABEL</string>"
            echo '  <key>ProgramArguments</key><array>'
            echo '    <string>/usr/bin/find</string>'
            while IFS= read -r a; do
                a="${a//&/&amp;}"; a="${a//</&lt;}"; a="${a//>/&gt;}"
                echo "    <string>$a</string>"
            done < <(find_args "$dir")
            echo '  </array>'
            # Каждый час и при входе в систему: выключенный ноутбук пропускает
            # прогоны, а заснувший launchd догоняет при пробуждении.
            echo '  <key>StartCalendarInterval</key><dict><key>Minute</key><integer>7</integer></dict>'
            echo '  <key>RunAtLoad</key><true/>'
            echo "  <key>StandardErrorPath</key><string>$dir/retention.log</string>"
            echo "  <key>StandardOutPath</key><string>$dir/retention.log</string>"
            echo '</dict></plist>'
        } > "$plist"
        plutil -lint "$plist" >/dev/null
        launchctl bootout "gui/$(id -u)/$AGENT_LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$plist"
        launchctl print "gui/$(id -u)/$AGENT_LABEL" >/dev/null
        echo "==> Автоудаление копий старше 14 суток: агент $AGENT_LABEL работает (каждый час)."
        ;;
    *) usage ;;
esac
