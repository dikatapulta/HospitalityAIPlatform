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
   ENTRYPOINT образа; и единственное, что заметит его пропажу, — этот тест:
   ни один другой прогон CI внутрь `ops/Dockerfile` не смотрит. Поэтому тест
   читает файлы репозитория, а не поведение кода, — это осознанно.

Файл лежит в `tests/`, а не рядом с проверками: сверяются composition root'ы и
ops-файлы образа, то есть уровень выше любого отдельного слоя (та же причина,
что у соседнего `test_llm_model_startup.py`). БД и сеть тестам не нужны.
"""

from __future__ import annotations

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


@pytest.fixture
def zero_attempts(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


def test_image_checks_configuration_before_running_its_command() -> None:
    """ENTRYPOINT образа гоняет preflight и лишь потом отдаёт управление команде.

    Пропажа любой из трёх строк возвращает дыру #267 молча: контейнер продолжит
    подниматься, а негодная конфигурация снова обернётся спин-лупом детей под
    `--workers`/`--reload` вместо мгновенного падения.
    """
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    entrypoint = _ENTRYPOINT.read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["/app/ops/entrypoint.sh"]' in dockerfile
    assert "python -m hospitality.preflight" in entrypoint
    # exec, а не запуск дочерним процессом: иначе PID 1 остаётся шелл и SIGTERM
    # от `docker stop` до uvicorn/воркера не доходит.
    assert 'exec "$@"' in entrypoint
