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

echo "==> Ставлю age — шифрование бэкапов БД (issue #81, spec 0024)..."
# Без age бэкап не создаётся вовсе (fail-closed), поэтому ставим сразу при
# подготовке сервера. apt — основной путь (Ubuntu 24.04+/Debian 13+); если
# пакета нет, кладём статический бинарник из релиза (sha256 сверяется; хеш —
# для linux-amd64, на другой архитектуре `age --version` ниже упадёт заметно).
AGE_VERSION="1.3.1"
AGE_SHA256="bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377"
if ! command -v age >/dev/null 2>&1; then
    if ! (apt-get update -qq && apt-get install -y age); then
        echo "    apt-пакета age нет — ставлю статический бинарник v$AGE_VERSION..."
        tmp="$(mktemp -d)"
        curl -fsSL -o "$tmp/age.tgz" \
            "https://github.com/FiloSottile/age/releases/download/v$AGE_VERSION/age-v$AGE_VERSION-linux-amd64.tar.gz"
        echo "$AGE_SHA256  $tmp/age.tgz" | sha256sum -c -
        tar -xzf "$tmp/age.tgz" -C "$tmp"
        install -m 0755 "$tmp/age/age" /usr/local/bin/age
        rm -rf "$tmp"
    fi
fi
age --version

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
       (не забудьте BACKUP_AGE_RECIPIENT — без него бэкапы не создаются, issue #81)
    3) Добавьте в GitHub секреты STAGING_SSH_HOST/USER/KEY — деплой активируется сам
EOF
