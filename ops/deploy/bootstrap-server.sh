#!/usr/bin/env bash
# Разовая подготовка чистого VPS под staging (Task 0006, FOUNDATION §10.11).
# Идемпотентен: повторный запуск ничего не ломает. Для Debian/Ubuntu.
# Полная процедура (создать сервер, ключи, секреты) — docs/runbooks/deploy.md.
#
#   sudo DEPLOY_USER=deploy ./bootstrap-server.sh
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER:-deploy}"
DEPLOY_DIR="${DEPLOY_DIR:-/opt/hospitality}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Запускать от root (sudo)." >&2
    exit 1
fi

echo "==> Ставлю Docker Engine + compose-плагин (официальный скрипт)..."
if ! command -v docker >/dev/null 2>&1; then
    curl -fsSL https://get.docker.com | sh
fi

echo "==> Создаю пользователя деплоя '$DEPLOY_USER' (без пароля, только ключ)..."
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$DEPLOY_USER"
fi
usermod -aG docker "$DEPLOY_USER"   # чтобы деплой работал с docker без sudo

echo "==> Готовлю каталог деплоя $DEPLOY_DIR..."
mkdir -p "$DEPLOY_DIR"
chown -R "$DEPLOY_USER":"$DEPLOY_USER" "$DEPLOY_DIR"

echo "==> Базовый firewall (ufw): SSH + порт приложения..."
if command -v ufw >/dev/null 2>&1; then
    ufw allow OpenSSH || true
    ufw allow 8000/tcp || true   # staging-приложение; в проде — за reverse-proxy с TLS
    yes | ufw enable || true
fi

cat <<EOF

==> Готово. Дальше (см. docs/runbooks/deploy.md):
    1) Положите публичный SSH-ключ CI в /home/$DEPLOY_USER/.ssh/authorized_keys
    2) В $DEPLOY_DIR создайте .env из ops/deploy/.env.staging.example и заполните секреты
    3) Добавьте в GitHub секреты STAGING_SSH_HOST/USER/KEY — деплой активируется сам
EOF
