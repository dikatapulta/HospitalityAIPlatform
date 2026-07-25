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
# и функцию staging_ssh "<cmd>".

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

staging_ssh() {
    # ~ в пути ключа раскрывается вручную: значение пришло из .env без шелла.
    if [ -n "$STAGING_SSH_KEY" ]; then
        ssh -i "${STAGING_SSH_KEY/#\~/$HOME}" "$STAGING_SSH_USER@$STAGING_HOST" "$@"
    else
        ssh "$STAGING_SSH_USER@$STAGING_HOST" "$@"
    fi
}

staging_scp_from() {
    # staging_scp_from <удалённый путь> <локальный путь>
    if [ -n "$STAGING_SSH_KEY" ]; then
        scp -i "${STAGING_SSH_KEY/#\~/$HOME}" "$STAGING_SSH_USER@$STAGING_HOST:$1" "$2"
    else
        scp "$STAGING_SSH_USER@$STAGING_HOST:$1" "$2"
    fi
}
