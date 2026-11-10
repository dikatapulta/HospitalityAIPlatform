"""Зависимости ставятся строго по `uv.lock` (#305, ADR-026).

Три щита:

* лок совпадает с `pyproject.toml` — правку зависимостей без `make lock` ловит локальный
  `make check`, а не только шаг установки в CI;
* `ops/install-deps.sh` отказывается ставить по устаревшему локу — прогоном скрипта;
  флаг `--locked`, на котором это держится, — сверкой строки команды (без сети uv
  отвечает на устаревший лок одинаково с флагом и без);
* в CI, образе и Makefile нет `pip install` в обход скрипта: новый job с голым
  `pip install -e .` снова брал бы с PyPI то, что лежит там в день прогона.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "ops" / "install-deps.sh"
_NO_UV_HINT = "uv не установлен в этом окружении — пересоздай его: make venv (ADR-026)"


def _uv_available() -> bool:
    probe = subprocess.run(
        [sys.executable, "-m", "uv", "--version"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0


def test_lock_matches_pyproject() -> None:
    assert _uv_available(), _NO_UV_HINT
    result = subprocess.run(
        [sys.executable, "-m", "uv", "lock", "--check", "--offline"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        "uv.lock не совпадает с pyproject.toml — выполни make lock и закоммить uv.lock:\n"
        + result.stderr
    )


def test_install_script_refuses_stale_lock(tmp_path: Path) -> None:
    assert _uv_available(), _NO_UV_HINT
    (tmp_path / "ops").mkdir()
    shutil.copy(_SCRIPT, tmp_path / "ops" / "install-deps.sh")
    shutil.copy(_ROOT / "uv.lock", tmp_path / "uv.lock")
    lock_before = (tmp_path / "uv.lock").read_bytes()
    pyproject = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    anchor = "dependencies = [\n"
    assert pyproject.count(anchor) == 1
    stale = pyproject.replace(anchor, anchor + '    "necturn-lock-probe-305>=1",\n')
    (tmp_path / "pyproject.toml").write_text(stale, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHON": sys.executable,
        # Без сети: uv уже стоит, а устаревший лок обязан останавливать установку и без неё.
        "UV_OFFLINE": "1",
        "PIP_NO_INDEX": "1",
    }

    result = subprocess.run(
        ["sh", str(tmp_path / "ops" / "install-deps.sh"), "dev"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "make lock" in result.stderr, result.stderr
    # Лок не переписан и до pip дело не дошло: «No broken requirements» печатает pip check.
    assert (tmp_path / "uv.lock").read_bytes() == lock_before
    assert "No broken requirements" not in result.stdout


def test_install_script_exports_with_locked() -> None:
    """`--locked` сверяется строкой: без сети uv не отличает его отсутствие (см. выше).

    Без флага `uv export` молча пересобрал бы устаревший лок и поставил непроверенные
    версии — ровно то, от чего защищает #305.
    """
    exports = [
        shlex.split(line)
        for line in _code_lines(_SCRIPT)
        if line.startswith('if ! "$PYTHON" -m uv export ')
    ]
    assert len(exports) == 1, "команда uv export в ops/install-deps.sh не найдена одной строкой"
    assert "--locked" in exports[0]
    assert "--frozen" not in exports[0]


def test_install_script_rejects_unknown_mode() -> None:
    # PYTHON указывает в никуда: тронь скрипт интерпретатор до разбора режима — код был бы 127.
    env = {**os.environ, "PYTHON": "/nonexistent/python"}
    result = subprocess.run(
        ["sh", str(_SCRIPT), "prod"], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 2, result.stderr
    assert "runtime|dev" in result.stderr


def _code_lines(path: Path) -> list[str]:
    """Строки файла без комментариев — подстрока в комментарии щит не обманет."""
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return lines


def _install_sites() -> list[Path]:
    workflows = sorted((_ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows, "не найдено ни одного workflow — разбор путей сломан"
    return [*workflows, _ROOT / "ops" / "Dockerfile", _ROOT / "Makefile"]


def test_no_pip_install_bypassing_lock() -> None:
    bypass = [
        f"{path.relative_to(_ROOT)}: {line}"
        for path in _install_sites()
        for line in _code_lines(path)
        if "pip install" in line or "uv sync" in line
    ]
    assert not bypass, "Установка в обход ops/install-deps.sh (ADR-026):\n" + "\n".join(bypass)


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("ops/Dockerfile", "RUN ./ops/install-deps.sh runtime"),
        ("ops/Dockerfile", "RUN ./ops/install-deps.sh dev"),
        ("Makefile", "PYTHON=$(BIN)/python ./ops/install-deps.sh dev"),
        (".github/workflows/ci.yml", "run: ./ops/install-deps.sh dev"),
        (".github/workflows/ci.yml", "run: ./ops/install-deps.sh runtime"),
        (".github/workflows/smoke.yml", "run: ./ops/install-deps.sh dev"),
    ],
)
def test_install_sites_call_script(relative: str, expected: str) -> None:
    # Строка целиком. Job без установки упал бы и сам, а production-образ без строки
    # `runtime` собрался бы без зависимостей: CI собирает только цель dev (она ставит
    # всё заново), production — уже деплой. Этот случай и держит щит.
    assert expected in _code_lines(_ROOT / relative)
