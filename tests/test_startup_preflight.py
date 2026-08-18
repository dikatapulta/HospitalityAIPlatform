"""Fail-fast конфигурации на старте процесса (issue #267, issue #273).

Проверок здесь две, и обе — про одно: негодная настройка обязана валить СТАРТ,
а не всплывать в рантайме у гостя.

1. `LLM_MAX_ATTEMPTS=0` (issue #273). При нуле цикл попыток в `complete()` не
   выполняется ни разу, `result` остаётся `None`, и вызов падает на
   `assert result is not None` — гость получает 500 вместо кода каталога (R-8).
   Лечит это граница на поле конфигурации, а не защита в коде.

2. Образ прогоняет проверки ДО рабочей команды (issue #267). `SystemExit` роняет
   процесс, в котором исполняется, а `uvicorn --workers N` и `uvicorn --reload`
   держат над рабочим процессом родителя-супервизора: тот переживает смерть
   ребёнка и перезапускает его в цикле. Единственное, что закрывает дыру, —
   ENTRYPOINT образа, и проверяется он ПОВЕДЕНИЕМ: `ops/entrypoint.sh`
   запускается настоящим `sh` прямо из репозитория, Docker для этого не нужен —
   скрипт не знает, что исполняется не в контейнере.

   Проверкой формы (подстрока в файле) эта роль не закрывается, и это выяснено
   дорого: прежняя версия теста оставалась зелёной на скрипте, из которого
   удалён `set -e`, — то есть на дыре #267, открытой целиком, потому что без
   `set -e` шелл идёт дальше и `exec`'ает команду при провалившемся preflight
   (ревью PR #282). Пара «годная конфигурация → команда выполнена / негодная →
   не выполнена» краснеет на снятии `set -e`, на снятии самого вызова preflight
   и на их перестановке. Четвёртая несущая строка — `exec` — этой парой не
   покрыта: без него команда всё равно выполняется, меняется только владелец
   PID 1, — поэтому у неё отдельный тест на тождество PID.

   Каждая правка этого файла проверяется тем же способом, каким найдена дыра в
   прежней версии: скрипт калечится, тест обязан покраснеть. Молчащий тест на
   ops-файлы дороже отсутствующего — он выдаёт себя за щит.

Файл лежит в `tests/`, а не рядом с проверками: сверяются composition root'ы и
ops-файлы образа, то есть уровень выше любого отдельного слоя (та же причина,
что у соседнего `test_llm_model_startup.py`). БД и сеть тестам не нужны.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from hospitality.preflight import run_preflight
from hospitality.shared.config import get_settings

# tests/ лежит в корне репозитория — родитель этого файла есть корень.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DOCKERFILE = _REPO_ROOT / "ops" / "Dockerfile"
_ENTRYPOINT = _REPO_ROOT / "ops" / "entrypoint.sh"

# Метка в stdout: она либо есть (команда выполнена), либо нет (до `exec` не дошло).
_MARKER = "COMMAND RAN"
# Годная модель — из прайс-листа `MODEL_PRICING_USD_PER_MTOK`; негодная — тот самый
# датированный id из issue #137, который и был поводом для проверки.
_GOOD_MODEL = "claude-sonnet-5"
_BAD_MODEL = "claude-sonnet-5-20250929"


@pytest.fixture
def zero_attempts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _entrypoint_env(**env: str) -> dict[str, str]:
    """Окружение для запуска скрипта вне контейнера.

    PATH ведёт на интерпретатор, под которым идут сами тесты: скрипт зовёт
    `python` по имени, а в локальном прогоне это шим pyenv без установленного
    пакета. Без подмены отрицательный тест «проходил» бы на
    `ModuleNotFoundError` — негодная конфигурация ни при чём; ловит это парный
    положительный тест, который на таком PATH покраснел бы.
    """
    environment = {**os.environ, **env}
    environment["PATH"] = os.pathsep.join(
        [str(Path(sys.executable).parent), environment.get("PATH", "")]
    )
    return environment


def _run_entrypoint(*command: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Гоняет `ops/entrypoint.sh` настоящим шеллом, как это делает контейнер."""
    return subprocess.run(
        ["sh", str(_ENTRYPOINT), *command],
        capture_output=True,
        text=True,
        env=_entrypoint_env(**env),
        cwd=_REPO_ROOT,
        timeout=120,
    )


def test_zero_attempts_is_rejected_as_configuration_error(zero_attempts: None) -> None:
    """Ноль попыток не доживает до вызова провайдера — падает на сборке Settings."""
    with pytest.raises(ValidationError) as error:
        run_preflight()

    message = str(error.value)
    # Сообщение обязано вести к исправлению: какое поле и какая граница нарушена.
    assert "llm_max_attempts" in message
    assert "greater than or equal to 1" in message


def test_preflight_passes_on_default_configuration() -> None:
    """Дефолт конфигурации годен — иначе предыдущий тест зеленел бы впустую."""
    assert get_settings().llm_max_attempts >= 1

    run_preflight()  # не бросает — иначе тест упал бы здесь


def test_entrypoint_runs_the_command_on_good_configuration() -> None:
    """Годная конфигурация — команда получает управление через `exec`."""
    result = _run_entrypoint("echo", _MARKER, LLM_MODEL=_GOOD_MODEL)

    assert result.returncode == 0, result.stderr
    assert _MARKER in result.stdout


def test_entrypoint_refuses_to_run_the_command_on_bad_configuration() -> None:
    """Негодная конфигурация — контейнер выходит с кодом 1, команда не стартует.

    Это воспроизведение самой дыры #267 (R-7): без `set -e` или без вызова
    preflight в скрипте команда выполнится и тест покраснеет на метке в stdout.
    """
    result = _run_entrypoint("echo", _MARKER, LLM_MODEL=_BAD_MODEL)

    assert result.returncode != 0
    assert _MARKER not in result.stdout
    # Сообщение ведёт к исправлению .env, а не к чтению исходников (issue #137).
    assert "LLM_MODEL" in result.stderr


def test_entrypoint_hands_its_own_process_to_the_command() -> None:
    """`exec`, а не запуск ребёнком: команда становится тем же процессом.

    В контейнере это PID 1: без `exec` SIGTERM от `docker stop` получил бы шелл,
    а uvicorn/воркер — нет, и остановка шла бы по таймауту вместо корректной.
    Проверяется тождеством PID — `exec` заменяет процесс шелла собой, обычный
    запуск порождает ребёнка с другим номером (снятие `exec` тест краснит).
    """
    process = subprocess.Popen(
        ["sh", str(_ENTRYPOINT), "sh", "-c", "echo $$"],
        stdout=subprocess.PIPE,
        text=True,
        env=_entrypoint_env(LLM_MODEL=_GOOD_MODEL),
        cwd=_REPO_ROOT,
    )
    stdout, _ = process.communicate(timeout=120)

    assert process.returncode == 0
    assert stdout.strip() == str(process.pid)


def test_entrypoint_refuses_to_exit_silently_without_a_command() -> None:
    """Пустой `$@` — отказ, а не тихий выход с кодом 0 (ревью PR #282)."""
    result = _run_entrypoint(LLM_MODEL=_GOOD_MODEL)

    assert result.returncode != 0
    assert result.stderr.strip() != ""


def test_dockerfile_wires_the_entrypoint() -> None:
    """Скрипт защищает контейнер, только если он и правда ENTRYPOINT образа.

    Сверка по строке целиком, а не по подстроке: подстрока истинна и внутри
    закомментированной строки (ревью PR #282).
    """
    lines = [line.strip() for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines()]

    assert 'ENTRYPOINT ["/app/ops/entrypoint.sh"]' in lines
