"""Issue #374 и #911: снимок отказов модели по тенантам — по журналу `llm_call_log`.

Строки журнала пишутся напрямую, с явным `created_at`: проверяется арифметика
серий («после последнего успешного», «в текущих UTC-сутках»), а не шлюз —
путь шлюза до журнала покрывает test_gateway.py.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY

from hospitality.ai.gateway import service
from hospitality.ai.gateway.api import refresh_call_outcome_metrics
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.purposes import LlmPurpose
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.metrics import (
    LlmCallOutcomes,
    set_llm_call_outcomes,
    set_llm_mentor_outcomes,
)
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database")

OK = LlmCallStatus.OK
ERROR = LlmCallStatus.ERROR
TIMEOUT = LlmCallStatus.TIMEOUT
BUDGET = LlmCallStatus.BUDGET_EXCEEDED

MENTOR_MODEL = "gpt-6-luna"
EMBEDDING_MODEL = "text-embedding-3-small"
# «Сейчас» шлюза в этих тестах: строки журнала пишутся «за N минут до сейчас», а
# отказы по бюджету считаются с полуночи UTC — по настенным часам первые минуты
# суток уводили часть строк во вчера, и тесты краснели без регресса (#1037).
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _noon_utc() -> Iterator[None]:
    # Своя MonkeyPatch, как у `_frozen_ratelimit_clock` (tests/conftest.py): автофикстура
    # с `monkeypatch` откатывала бы POSTGRES_DB после удаления тестовой БД.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(service, "utc_now", lambda: NOON)
        yield


async def _write_calls(
    tenant_id: uuid.UUID,
    statuses: list[LlmCallStatus],
    *,
    purpose: LlmPurpose = "guest",
    model: str = "claude-sonnet-5",
) -> None:
    """Журнал тенанта: статусы по порядку, с шагом в минуту до `NOON`."""
    await _write_mixed(tenant_id, [(status, model) for status in statuses], purpose=purpose)


async def _write_mixed(
    tenant_id: uuid.UUID, calls: list[tuple[LlmCallStatus, str]], *, purpose: LlmPurpose
) -> None:
    """Как `_write_calls`, но у каждой строки своя модель."""
    now = NOON
    with tenant_context(tenant_id):
        async with session_scope() as session:
            session.add_all(
                LlmCallLog(
                    provider="mock",
                    model=model,
                    purpose=purpose,
                    prompt_hash="0" * 64,
                    status=status,
                    created_at=now - timedelta(minutes=len(calls) - index),
                )
                for index, (status, model) in enumerate(calls)
            )


async def _write_calls_at(
    tenant_id: uuid.UUID, calls: list[tuple[LlmCallStatus, datetime]]
) -> None:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            session.add_all(
                LlmCallLog(
                    provider="mock",
                    model="claude-sonnet-5",
                    prompt_hash="0" * 64,
                    status=status,
                    created_at=created_at,
                )
                for status, created_at in calls
            )


@pytest.fixture
def mentor_models() -> Iterator[None]:
    """Модели наставника на деплойменте. Свой `MonkeyPatch.context()` — по той же
    причине, что в test_embeddings.py: откат окружения не зависит от порядка фикстур."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL_MENTOR", MENTOR_MODEL)
        patch.setenv("LLM_EMBEDDING_MODEL", EMBEDDING_MODEL)
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


def _outcomes(tenant_id: uuid.UUID, *, mentor: bool = False) -> LlmCallOutcomes | None:
    failure_metric = "llm_mentor_failure_streak" if mentor else "llm_provider_failure_streak"
    rejection_metric = (
        "llm_mentor_budget_rejection_streak" if mentor else "llm_budget_rejection_streak"
    )
    labels = {"tenant_id": str(tenant_id)}
    errors = REGISTRY.get_sample_value(failure_metric, labels | {"status": "error"})
    timeouts = REGISTRY.get_sample_value(failure_metric, labels | {"status": "timeout"})
    rejections = REGISTRY.get_sample_value(rejection_metric, labels)
    if errors is None or timeouts is None or rejections is None:
        return None
    return LlmCallOutcomes(
        provider_errors=int(errors),
        provider_timeouts=int(timeouts),
        budget_rejections=int(rejections),
    )


async def test_provider_failures_are_counted_after_last_success(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Серия — неуспешные вызовы ПОСЛЕ последнего успешного; отель без отказов
    в снимке есть нулями (иначе алерт о нём некому было бы погасить)."""
    tenant_a, tenant_b = two_tenants
    await _write_calls(tenant_a, [ERROR, OK, ERROR, TIMEOUT, ERROR])

    await refresh_call_outcome_metrics()

    assert _outcomes(tenant_a) == LlmCallOutcomes(
        provider_errors=2, provider_timeouts=1, budget_rejections=0
    )
    assert _outcomes(tenant_b) == LlmCallOutcomes(
        provider_errors=0, provider_timeouts=0, budget_rejections=0
    )


async def test_success_resets_provider_streak(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    await _write_calls(tenant_a, [ERROR, ERROR, ERROR, OK])

    await refresh_call_outcome_metrics()

    outcomes = _outcomes(tenant_a)
    assert outcomes is not None and outcomes.provider_errors == 0


async def test_tenant_without_any_success_counts_whole_journal(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Ключ отозван с первого дня — успешного вызова нет вовсе."""
    tenant_a, _ = two_tenants
    await _write_calls(tenant_a, [ERROR, ERROR])

    await refresh_call_outcome_metrics()

    outcomes = _outcomes(tenant_a)
    assert outcomes is not None and outcomes.provider_errors == 2


async def test_budget_rejections_do_not_break_or_extend_provider_streak(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Отказ по бюджету до провайдера не доходил: о провайдере он не говорит."""
    tenant_a, _ = two_tenants
    await _write_calls(tenant_a, [ERROR, BUDGET, BUDGET, ERROR])

    await refresh_call_outcome_metrics()

    assert _outcomes(tenant_a) == LlmCallOutcomes(
        provider_errors=2, provider_timeouts=0, budget_rejections=0
    )


async def test_budget_rejections_are_counted_after_last_passed_call(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Лимит подняли — вызов прошёл — серия отказов обнулилась; отказы после
    него — новая серия."""
    tenant_a, _ = two_tenants
    await _write_calls(tenant_a, [OK, BUDGET, BUDGET, OK, BUDGET, BUDGET, BUDGET])

    await refresh_call_outcome_metrics()

    outcomes = _outcomes(tenant_a)
    assert outcomes is not None and outcomes.budget_rejections == 3


async def test_yesterdays_budget_rejections_do_not_count(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Бюджет дневной: с полуночи UTC вчерашние отказы о сегодняшнем дне не
    говорят — серия обнуляется, и алерт гаснет в новых сутках."""
    tenant_a, _ = two_tenants
    day_start = NOON.replace(hour=0)
    await _write_calls_at(
        tenant_a,
        [(BUDGET, day_start - timedelta(minutes=5)), (BUDGET, day_start - timedelta(minutes=1))],
    )

    await refresh_call_outcome_metrics()

    outcomes = _outcomes(tenant_a)
    assert outcomes is not None and outcomes.budget_rejections == 0


async def test_outcomes_snapshot_is_empty_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Недоступная БД не роняет /metrics и не выдаёт старый снимок за нынешний:
    метрики стираются, и для алертера это «не знаю» (канон #103)."""
    stale_tenant = uuid.uuid4()
    stale = {
        str(stale_tenant): LlmCallOutcomes(
            provider_errors=5, provider_timeouts=0, budget_rejections=0
        )
    }
    set_llm_call_outcomes(stale)
    set_llm_mentor_outcomes(stale)

    def explode() -> None:
        raise RuntimeError("postgres is down")

    monkeypatch.setattr("hospitality.ai.gateway.outcomes.platform_session_scope", explode)
    await refresh_call_outcome_metrics()

    assert _outcomes(stale_tenant) is None
    assert _outcomes(stale_tenant, mentor=True) is None


# ---------------------------------------------------------------------------
# «Наставник» (issue #911)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mentor_models")
async def test_purposes_do_not_mix(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Отказы наставника — в его метриках и не в гостевых, и наоборот (ADR-024 §7):
    гостевой алерт про наставника будил бы словами «бот не отвечает гостям»."""
    tenant_a, tenant_b = two_tenants
    await _write_calls(tenant_a, [ERROR, ERROR, ERROR], purpose="mentor", model=MENTOR_MODEL)
    await _write_calls(tenant_b, [TIMEOUT, TIMEOUT])

    await refresh_call_outcome_metrics()

    assert _outcomes(tenant_a, mentor=True) == LlmCallOutcomes(
        provider_errors=3, provider_timeouts=0, budget_rejections=0
    )
    assert _outcomes(tenant_a) == LlmCallOutcomes(
        provider_errors=0, provider_timeouts=0, budget_rejections=0
    )
    assert _outcomes(tenant_b, mentor=True) == LlmCallOutcomes(
        provider_errors=0, provider_timeouts=0, budget_rejections=0
    )


@pytest.mark.usefixtures("mentor_models")
async def test_mentor_streak_is_per_model(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Снята модель ответа: эмбеддинг вопроса проходит перед каждым её отказом.
    Общая серия обнулялась бы на каждом вопросе, серия модели — растёт."""
    tenant_a, _ = two_tenants
    await _write_mixed(
        tenant_a,
        [(OK, EMBEDDING_MODEL), (ERROR, MENTOR_MODEL)] * 3,
        purpose="mentor",
    )

    await refresh_call_outcome_metrics()

    assert _outcomes(tenant_a, mentor=True) == LlmCallOutcomes(
        provider_errors=3, provider_timeouts=0, budget_rejections=0
    )


@pytest.mark.usefixtures("mentor_models")
async def test_mentor_budget_rejections_are_per_model(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """У края потолка дешёвый эмбеддинг проходит, дорогой ответ — нет: серия отказов
    ответа не обнуляется эмбеддингом между ними."""
    tenant_a, _ = two_tenants
    await _write_mixed(
        tenant_a,
        [(OK, MENTOR_MODEL)] + [(OK, EMBEDDING_MODEL), (BUDGET, MENTOR_MODEL)] * 2,
        purpose="mentor",
    )

    await refresh_call_outcome_metrics()

    outcomes = _outcomes(tenant_a, mentor=True)
    assert outcomes is not None and outcomes.budget_rejections == 2


@pytest.mark.usefixtures("mentor_models")
async def test_mentor_ignores_models_not_on_deployment(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Модель сменили после серии отказов: прежняя больше не зовётся, и её серия
    держала бы алерт вечно — считаются только модели из настроек."""
    tenant_a, _ = two_tenants
    await _write_calls(tenant_a, [ERROR, ERROR, ERROR], purpose="mentor", model="gpt-5-retired")
    await _write_calls(tenant_a, [OK], purpose="mentor", model=MENTOR_MODEL)

    await refresh_call_outcome_metrics()

    outcomes = _outcomes(tenant_a, mentor=True)
    assert outcomes is not None and outcomes.provider_errors == 0
