"""Подготовка нового сервера: каталог бэкапов — под `deploy` (issue #289).

Алертер монтирует каталог бэкапов хоста bind-mount'ом, а такой каталог, если
его нет, создаёт демон Docker — с владельцем `root`, и `backup.sh` под `deploy`
не пишет туда ни одного дампа. Поэтому `ops/deploy/bootstrap-server.sh` создаёт
каталог сам, **до** `chown -R` каталога деплоя.

Проверка поведением, как у соседних `test_pre_migrate_backup.py` и
`test_deploy_image_prune.py`: настоящий `bash` гоняет настоящий скрипт, а всё,
что требует root (`id -u`, `useradd`, `chown`, `ufw`, Docker), подменено
заглушками в `PATH`. Заглушка `chown` записывает, был ли каталог бэкапов в момент
вызова, — так тест краснеет и без строки `mkdir`, и при её переносе за `chown`.

Там же (#1028): наружу открыт только SSH — порт приложения скрипт не открывает и
снимает правило прежней версии; вход по паролю выключается, но только когда у root
уже есть ключ, — иначе сервер запер бы самого основателя.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BOOTSTRAP_SH = _REPO_ROOT / "ops" / "deploy" / "bootstrap-server.sh"

_STUBS = {
    # «root» для проверки в начале скрипта; пользователь deploy «уже есть».
    "id": '[ "$1" = "-u" ] && echo 0\nexit 0\n',
    "docker": "exit 0\n",
    "age": "echo v1.3.1\n",
    "useradd": 'echo "useradd $*" >> "$CALL_LOG"\n',
    "usermod": 'echo "usermod $*" >> "$CALL_LOG"\n',
    "ufw": 'echo "ufw $*" >> "$CALL_LOG"\n',
    # sshd -t — проверка настроек: отказ, если задан SSHD_T_FAIL; -T — итог настроек.
    "sshd": (
        'echo "sshd $*" >> "$CALL_LOG"\n'
        '[ "$1" = "-t" ] && [ -n "$SSHD_T_FAIL" ] && exit 1\n'
        '[ "$1" = "-T" ] && echo "passwordauthentication no"\n'
        "exit 0\n"
    ),
    "systemctl": 'echo "systemctl $*" >> "$CALL_LOG"\n',
    "chown": (
        'for a in "$@"; do last="$a"; done\n'
        'if [ -d "$last/backups" ]; then seen=yes; else seen=no; fi\n'
        'echo "chown $* backups=$seen" >> "$CALL_LOG"\n'
    ),
    # Docker и age «уже стоят» — в сеть и apt скрипт ходить не должен.
    "curl": 'echo "unexpected curl" >&2\nexit 97\n',
    "apt-get": 'echo "unexpected apt-get" >&2\nexit 97\n',
}


def _bootstrap(
    tmp_path: Path, *, root_key: bool = False, sshd_t_fail: bool = False
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in _STUBS.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/sh\n" + body)
        stub.chmod(0o755)
    deploy_dir = tmp_path / "opt-hospitality"
    root_keys = tmp_path / "root-authorized_keys"
    if root_key:
        root_keys.write_text("ssh-ed25519 AAAAC3Nza founder\n")
    env = {
        # Без /usr/sbin: настоящие useradd/ufw/chown не должны найтись никогда.
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOME": str(tmp_path),
        "CALL_LOG": str(tmp_path / "calls.log"),
        "DEPLOY_USER": "deploy",
        "DEPLOY_DIR": str(deploy_dir),
        "SSHD_DROPIN_DIR": str(tmp_path / "sshd_config.d"),
        "ROOT_AUTHORIZED_KEYS": str(root_keys),
        "SSHD_T_FAIL": "1" if sshd_t_fail else "",
    }
    result = subprocess.run(
        ["bash", str(_BOOTSTRAP_SH)],
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
    )
    return result, deploy_dir


def test_backup_dir_is_created_before_chown_of_deploy_dir(tmp_path: Path) -> None:
    result, deploy_dir = _bootstrap(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert f"chown -R deploy:deploy {deploy_dir} backups=yes" in calls, calls
    # Алертер (uid 10001 в контейнере) входит в каталог как «прочие».
    mode = stat.S_IMODE((deploy_dir / "backups").stat().st_mode)
    assert mode & 0o005 == 0o005, oct(mode)


def test_rerun_on_prepared_server_still_succeeds(tmp_path: Path) -> None:
    """Скрипт идемпотентен (шапка): готовый каталог бэкапов повтор не роняет."""
    first, deploy_dir = _bootstrap(tmp_path)
    (deploy_dir / "backups" / "hospitality-20261103T030000Z.dump.age").write_text("x")

    second, _ = _bootstrap(tmp_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (deploy_dir / "backups" / "hospitality-20261103T030000Z.dump.age").exists()


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text().splitlines()


def test_firewall_opens_only_ssh_and_drops_old_app_port(tmp_path: Path) -> None:
    result, _ = _bootstrap(tmp_path)

    assert result.returncode == 0, result.stderr
    ufw = [c for c in _calls(tmp_path) if c.startswith("ufw ")]
    assert "ufw allow OpenSSH" in ufw, ufw
    assert "ufw delete allow 8000/tcp" in ufw, ufw
    assert not any(c.startswith("ufw allow 8000") for c in ufw), ufw


def test_password_login_is_disabled_when_root_has_a_key(tmp_path: Path) -> None:
    result, _ = _bootstrap(tmp_path, root_key=True)

    assert result.returncode == 0, result.stderr
    conf = (tmp_path / "sshd_config.d" / "10-keys-only.conf").read_text()
    assert "PasswordAuthentication no" in conf.splitlines(), conf
    calls = _calls(tmp_path)
    # Настройки проверены до перечитывания: битый файл не уронит sshd.
    assert calls.index("sshd -t") < calls.index("systemctl reload ssh"), calls
    assert "ВНИМАНИЕ" not in result.stderr, result.stderr


def test_password_login_stays_when_root_has_no_key(tmp_path: Path) -> None:
    result, _ = _bootstrap(tmp_path)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "sshd_config.d" / "10-keys-only.conf").exists()
    assert not any(c.startswith(("sshd ", "systemctl ")) for c in _calls(tmp_path))
    assert "вход по паролю НЕ выключен" in result.stderr, result.stderr


def test_rejected_sshd_config_is_rolled_back(tmp_path: Path) -> None:
    result, _ = _bootstrap(tmp_path, root_key=True, sshd_t_fail=True)

    assert result.returncode == 1, result.stdout + result.stderr
    assert not (tmp_path / "sshd_config.d" / "10-keys-only.conf").exists()
    assert not any(c.startswith("systemctl ") for c in _calls(tmp_path))
