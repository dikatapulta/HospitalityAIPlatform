#!/usr/bin/env bash
# Общий помощник SSH-доступа к staging с машины основателя (Task 0019).
# Подключается через `source`; сам по себе не запускается.
#
# Читает из окружения или локального .env (в репозитории значений нет, §11):
#   STAGING_HOST     — адрес сервера (обязателен);
#   STAGING_SSH_KEY  — путь к приватному ключу (пусто = ключи ssh-агента);
#   STAGING_SSH_USER — пользователь (по умолчанию deploy);
#   STAGING_BASE_URL — внешний адрес приложения (пусто = http://$STAGING_HOST:8000,
#                      который после Cloudflare-туннеля закрыт — см. .env.example).
# Наружу отдаёт: STAGING_HOST, STAGING_SSH_USER, STAGING_BASE_URL
# и функции staging_ssh "<cmd>" / staging_scp_from <откуда> <куда>.
#
# Все подключения мультиплексируются (issue #81): на сервере `ufw limit` банит
# частые НОВЫЕ подключения (6 за 30 с), а процедуры runbook'ов делают их подряд
# (взять дамп + подать его в pg_restore + сверить счётчики) — без общего
# соединения это ловится баном ровно в аварии, когда времени нет. Первый вызов
# поднимает мастер-соединение, остальные идут внутри него.

_read_env_var() {
    # Значение из локального .env, если переменная не задана в окружении.
    local name="$1"
    grep -E "^${name}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true
}

STAGING_HOST="${STAGING_HOST:-$(_read_env_var STAGING_HOST)}"
STAGING_SSH_KEY="${STAGING_SSH_KEY:-$(_read_env_var STAGING_SSH_KEY)}"
STAGING_SSH_USER="${STAGING_SSH_USER:-deploy}"
STAGING_BASE_URL="${STAGING_BASE_URL:-$(_read_env_var STAGING_BASE_URL)}"

if [ -z "$STAGING_HOST" ]; then
    echo "STAGING_HOST не задан: добавь в .env строки STAGING_HOST=<IP staging>" >&2
    echo "и STAGING_SSH_KEY=<путь к ключу> (см. .env.example и docs/runbooks/restore.md)." >&2
    exit 1
fi

# Общие опции всех подключений: мультиплексирование + ключ (если задан).
# %C в пути сокета — хеш (пользователь, хост, порт): при смене STAGING_HOST
# (случай Б восстановления — новый сервер) команда не уйдёт по живому сокету на
# старый адрес. Сокет в ~/.ssh, а не в общедоступном /tmp; имя короткое, потому
# что ssh не принимает ControlPath длиннее 104 байт.
_STAGING_SSH_OPTS=(
    -o ControlMaster=auto
    -o "ControlPath=$HOME/.ssh/.hosp-mux-%C"
    -o ControlPersist=60s
)
# ~ в пути ключа раскрывается вручную: значение пришло из .env без шелла.
if [ -n "$STAGING_SSH_KEY" ]; then
    _STAGING_SSH_OPTS+=(-i "${STAGING_SSH_KEY/#\~/$HOME}")
fi

staging_ssh() {
    ssh "${_STAGING_SSH_OPTS[@]}" "$STAGING_SSH_USER@$STAGING_HOST" "$@"
}

staging_scp_from() {
    # staging_scp_from <удалённый путь> <локальный путь>
    scp "${_STAGING_SSH_OPTS[@]}" "$STAGING_SSH_USER@$STAGING_HOST:$1" "$2"
}
