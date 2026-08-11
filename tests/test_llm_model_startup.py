"""Fail-fast валидация `LLM_MODEL` по прайс-листу (issue #137, аудит 21.07 H5).

Критерий приёмки issue: процесс с моделью вне `MODEL_PRICING_USD_PER_MTOK` не
поднимается — ни API, ни воркер, — и падает ДО первого запроса; при модели из
прайс-листа поведение прежнее. БД тестам не нужна: проверка стоит раньше любого
обращения к ней (это здесь и доказывается).

Файл лежит в `tests/`, а не рядом с самой проверкой (`ai/gateway/tests/`):
проверяются оба composition root'а, а импорт `create_app` из слоя `ai/` запретил
бы контракт 7 import-linter'а (кабинет персонала виден только корню).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI

from hospitality.ai.gateway.api import validate_configured_model
from hospitality.ai.gateway.service import MODEL_PRICING_USD_PER_MTOK
from hospitality.app import create_app
from hospitality.shared.config import get_settings
from hospitality.worker import run_worker

# Датированный идентификатор — самая правдоподобная ошибка: такие id печатает
# документация провайдера, а ключи прайс-листа — без даты (аудит 21.07, H5).
UNKNOWN_MODEL = "claude-sonnet-5-20250929"


@pytest.fixture
def unknown_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LLM_MODEL", UNKNOWN_MODEL)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def delivery_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Подменяет опрос outbox счётчиком вызовов — «первая итерация состоялась».

    Заодно избавляет тест воркера от БД: без подмены цикл ловил бы ошибку
    подключения (канон test_worker_iteration_survives_infrastructure_failure).
    """
    calls: list[int] = []

    async def spy(batch_size: int | None = None, max_attempts: int | None = None) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr("hospitality.worker.deliver_pending_events", spy)
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    yield calls
    get_settings.cache_clear()


def test_unknown_model_is_rejected_as_configuration_error(unknown_model: None) -> None:
    with pytest.raises(SystemExit) as error:
        validate_configured_model()

    message = str(error.value)
    # Сообщение обязано вести к исправлению: что не так, где и чем чинить.
    assert UNKNOWN_MODEL in message
    assert "LLM_MODEL" in message
    for known_model in MODEL_PRICING_USD_PER_MTOK:
        assert known_model in message


def test_configured_model_from_price_list_passes() -> None:
    """Дефолт конфигурации — модель из прайс-листа: старт не трогается."""
    assert get_settings().llm_model in MODEL_PRICING_USD_PER_MTOK

    validate_configured_model()  # не бросает — иначе тест упал бы здесь


def test_app_does_not_start_with_unknown_model(unknown_model: None) -> None:
    """Раньше это вскрывалось только 500-кой у первого гостя (H5)."""
    with pytest.raises(SystemExit):
        create_app()


def test_app_starts_with_model_from_price_list() -> None:
    assert isinstance(create_app(), FastAPI)


async def test_worker_does_not_start_with_unknown_model(
    unknown_model: None, delivery_spy: list[int]
) -> None:
    """Падение — до первой итерации: outbox не опрашивается, провайдер не зовётся."""
    with pytest.raises(SystemExit):
        await run_worker(iterations=1)

    assert delivery_spy == []


async def test_worker_starts_with_model_from_price_list(delivery_spy: list[int]) -> None:
    await run_worker(iterations=1)

    assert delivery_spy == [1]
