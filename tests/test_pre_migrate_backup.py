"""Снимок БД перед миграцией в деплое (issue #135) — проверка поведением.

Шаг живёт в двух shell-файлах: `ops/deploy/deploy.sh` зовёт канон
`ops/backup/backup.sh` с меткой `pre-migrate-<тег образа>` ДО
`alembic upgrade head`. Ценность шага целиком в двух свойствах, и оба —
поведенческие: снимок появляется **раньше** миграции, а не снявшийся снимок
**останавливает** деплой (fail-closed).

Проверкой формы (подстрока в файле) они не закрываются, и это в репозитории уже
выяснено дорого: тест на ops-файл оставался зелёным на скрипте, из которого
удалён `set -e` (ревью PR #282). Поэтому здесь настоящий `bash` гоняет настоящие
скрипты в каталоге, повторяющем `/opt/hospitality`: рядом лежат оба файла,
`.env` и compose-файл, а `docker` и `age` подменены заглушками в `PATH` —
скрипты не знают, что исполняются не на сервере.

Каждая правка этих скриптов проверяется тем же способом, каким находят дыру:
скрипт калечится (убрать вызов снимка, убрать `exit 1` после его провала,
переставить снимок после миграции), и тест обязан покраснеть. Молчащий тест на
ops-файлы дороже отсутствующего — он выдаёт себя за щит.

Файл лежит в `tests/`, а не рядом с кодом: проверяются ops-файлы деплоя, то есть
уровень выше любого слоя приложения (та же причина, что у соседнего
`test_startup_preflight.py`). БД, сеть и Docker тестам не нужны.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

# tests/ лежит в корне репозитория — родитель этого файла есть корень.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY_SH = _REPO_ROOT / "ops" / "deploy" / "deploy.sh"
_BACKUP_SH = _REPO_ROOT / "ops" / "backup" / "backup.sh"

# Образ, каким его передаёт CI: тег — полный git sha коммита.
_IMAGE = "ghcr.io/dikatapulta/hospitality-app:0123456789abcdef0123456789abcdef01234567"
# В имя дампа уходят первые 12 символов тега.
_EXPECTED_LABEL = "pre-migrate-0123456789ab"

# Заглушка docker: пишет каждый вызов в лог (по нему проверяется порядок шагов) и
# отвечает так же, как ответил бы настоящий на команды deploy.sh и backup.sh.
# FAKE_PG_DUMP_FAIL=1 роняет сам дамп — это и есть авария, ради которой шаг
# сделан fail-closed.
_FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$DOCKER_LOG"
case "$*" in
    *pg_dump*)
        if [ "${FAKE_PG_DUMP_FAIL:-}" = "1" ]; then
            echo "pg_dump: сервер не отвечает" >&2
            exit 1
        fi
        printf 'PGDMP-fake-archive\\n'
        ;;
    *pg_restore*)
        cat > /dev/null
        ;;
esac
exit 0
"""

# Заглушка age: настоящего ключа в тестах нет, но формат соблюдается — файл
# обязан начинаться заголовком age, иначе backup.sh его забракует.
_FAKE_AGE = """#!/bin/sh
out=""
src=""
while [ $# -gt 0 ]; do
    case "$1" in
        --recipient) shift ;;
        --output) out="$2"; shift ;;
        *) src="$1" ;;
    esac
    shift
done
{ printf 'age-encryption.org/v1\\n'; cat "$src"; } > "$out"
"""

_ENV_FILE = """POSTGRES_USER=hospitality
POSTGRES_DB=hospitality
BACKUP_AGE_RECIPIENT=age1testrecipientfortestsonly
"""

# Переменные, которыми окружение разработчика могло бы подменить проверяемое
# поведение: тест обязан зависеть только от того, что положил сам.
_ENV_KEYS_TO_DROP = (
    "COMPOSE_FILE",
    "ENV_FILE",
    "BACKUP_DIR",
    "BACKUP_RETENTION_DAYS",
    "BACKUP_AGE_RECIPIENT",
    "AGE_BIN",
    "APP_IMAGE",
    "SKIP_PRE_MIGRATE_BACKUP",
    "FAKE_PG_DUMP_FAIL",
)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def server(tmp_path: Path) -> Path:
    """Каталог, повторяющий `/opt/hospitality`: deploy.sh и backup.sh рядом.

    Рядом их кладёт scp-шаг деплоя (`.github/workflows/ci.yml`), и deploy.sh
    ищет канон именно так — `./backup.sh`.
    """
    shutil.copy(_DEPLOY_SH, tmp_path / "deploy.sh")
    shutil.copy(_BACKUP_SH, tmp_path / "backup.sh")
    (tmp_path / "docker-compose.staging.yml").write_text("# не читается: docker подменён\n")
    (tmp_path / ".env").write_text(_ENV_FILE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "docker", _FAKE_DOCKER)
    _write_executable(bin_dir / "age", _FAKE_AGE)
    return tmp_path


def _run(
    server: Path, script: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    process_env = dict(os.environ)
    for key in _ENV_KEYS_TO_DROP:
        process_env.pop(key, None)
    process_env["PATH"] = f"{server / 'bin'}{os.pathsep}{process_env['PATH']}"
    process_env["DOCKER_LOG"] = str(server / "docker.log")
    process_env.update(env or {})
    return subprocess.run(
        ["bash", script, *args],
        cwd=server,
        env=process_env,
        capture_output=True,
        text=True,
        # Битый байт в выводе не должен ронять тест исключением декодирования:
        # разбирать надо сообщение скрипта, а не кодировку сообщений bash.
        errors="replace",
        timeout=120,
    )


def _backup_env(server: Path) -> dict[str, str]:
    """Окружение прямого прогона backup.sh: иначе он смотрит в /opt/hospitality.

    Через deploy.sh те же три переменные подставляются сами — это его работа.
    """
    return {
        "COMPOSE_FILE": str(server / "docker-compose.staging.yml"),
        "ENV_FILE": str(server / ".env"),
        "BACKUP_DIR": str(server / "backups"),
    }


def _docker_calls(server: Path) -> list[str]:
    log = server / "docker.log"
    return log.read_text().splitlines() if log.exists() else []


def _index_of_call(server: Path, needle: str) -> int:
    """Номер первого вызова docker, содержащего подстроку; -1 — вызова не было."""
    for number, call in enumerate(_docker_calls(server)):
        if needle in call:
            return number
    return -1


def _dumps(server: Path) -> list[Path]:
    return sorted((server / "backups").glob("*.dump*")) if (server / "backups").exists() else []


def test_snapshot_taken_before_migration(server: Path) -> None:
    """Штатный деплой: дамп с говорящим именем есть, и он старше миграции."""
    result = _run(server, "deploy.sh", _IMAGE)

    assert result.returncode == 0, result.stderr
    dumps = _dumps(server)
    assert len(dumps) == 1, f"ожидался ровно один файл дампа, получено: {dumps}"
    dump = dumps[0]
    assert dump.name.startswith(f"{_EXPECTED_LABEL}-"), dump.name
    assert dump.name.endswith(".dump.age"), "боевое имя получает только шифртекст"

    # Критерий приёмки issue #135: имя файла видно в логе деплоя.
    assert dump.name in result.stdout

    # Шифрование — тем же каноном, что у штатного бэкапа (issue #81): открытым
    # дамп на диске не остаётся, а в файле лежит именно шифртекст.
    assert dump.read_text().startswith("age-encryption.org/v1")

    snapshot_call = _index_of_call(server, "pg_dump")
    migrate_call = _index_of_call(server, "alembic upgrade head")
    assert snapshot_call >= 0, "снимок не снимался вовсе"
    assert migrate_call >= 0, "миграция не запускалась"
    assert snapshot_call < migrate_call, "снимок обязан быть ДО миграции, иначе он бесполезен"


def test_failed_snapshot_stops_deploy_before_migration(server: Path) -> None:
    """Упавший pg_dump = стоп деплоя: схему БД никто не трогает (fail-closed)."""
    result = _run(server, "deploy.sh", _IMAGE, env={"FAKE_PG_DUMP_FAIL": "1"})

    assert result.returncode != 0, "деплой без снимка обязан упасть"
    assert _index_of_call(server, "alembic upgrade head") == -1, "миграция всё-таки поехала"
    assert "деплой остановлен" in result.stderr
    assert _dumps(server) == [], "недоснятый дамп не должен оставлять файла"


def test_missing_encryption_key_stops_deploy(server: Path) -> None:
    """Нет ключа age — нет снимка, а значит нет и миграции.

    Открытый дамп с перепиской гостей на диске недопустим (issue #81), поэтому
    backup.sh в этом случае падает — и деплой обязан упасть вместе с ним, а не
    мигрировать без страховки.
    """
    (server / ".env").write_text("POSTGRES_USER=hospitality\nPOSTGRES_DB=hospitality\n")

    result = _run(server, "deploy.sh", _IMAGE)

    assert result.returncode != 0
    assert _index_of_call(server, "alembic upgrade head") == -1
    assert _dumps(server) == [], "открытого дампа не остаётся даже как временного файла"


def test_skip_lever_migrates_without_snapshot(server: Path) -> None:
    """Аварийный рычаг: деплой едет без снимка, но говорит об этом громко."""
    result = _run(server, "deploy.sh", _IMAGE, env={"SKIP_PRE_MIGRATE_BACKUP": "1"})

    assert result.returncode == 0, result.stderr
    assert _dumps(server) == []
    assert _index_of_call(server, "alembic upgrade head") >= 0
    assert "ПРОПУЩЕН" in result.stderr


def test_nightly_backup_name_unchanged(server: Path) -> None:
    """Канон без аргумента остался прежним: cron продолжает класть hospitality-*.

    Имя ночного дампа зашито в `fetch.sh` (offsite-копия) и в runbook — метка
    в backup.sh появилась ради деплоя и трогать умолчание не вправе.
    """
    result = _run(server, "backup.sh", env=_backup_env(server))

    assert result.returncode == 0, result.stderr
    dumps = _dumps(server)
    assert len(dumps) == 1
    assert dumps[0].name.startswith("hospitality-")
    assert dumps[0].name.endswith(".dump.age")


def test_retention_covers_dumps_of_any_label(server: Path) -> None:
    """Retention — свойство каталога: прогон с любой меткой убирает всё старое.

    Иначе ночной cron не трогал бы дампы деплоя, а деплой — ночные, и каталог
    рос бы вечно за счёт «чужих» файлов.
    """
    backups = server / "backups"
    backups.mkdir()
    long_ago = time.time() - 30 * 24 * 3600
    stale = [
        backups / "hospitality-20260101T000000Z.dump.age",
        backups / "pre-migrate-deadbeef1234-20260101T000000Z.dump.age",
    ]
    for path in stale:
        path.write_text("старый дамп")
        os.utime(path, (long_ago, long_ago))

    result = _run(server, "backup.sh", "pre-migrate-abc123456789", env=_backup_env(server))

    assert result.returncode == 0, result.stderr
    for path in stale:
        assert not path.exists(), f"старый дамп пережил retention: {path.name}"
    fresh = _dumps(server)
    assert len(fresh) == 1
    assert fresh[0].name.startswith("pre-migrate-abc123456789-")


def test_bad_label_rejected_before_dump(server: Path) -> None:
    """Метка с путём или пробелом — стоп до дампа, а не файл со странным именем."""
    result = _run(server, "backup.sh", "../evil name", env=_backup_env(server))

    assert result.returncode != 0
    assert "метка" in result.stderr.lower()
    assert _dumps(server) == []
    assert _index_of_call(server, "pg_dump") == -1
