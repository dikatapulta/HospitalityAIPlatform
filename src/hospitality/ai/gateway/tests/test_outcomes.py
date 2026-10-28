"""Issue #374: снимок отказов модели по тенантам — по журналу `llm_call_log`.

Строки журнала пишутся напрямую, с явным `created_at`: проверяется арифметика
серий («после последнего успешного», «в текущих UTC-сутках»), а не шлюз —
путь шлюза до журнала покрывает test_gateway.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from prometheus_client import REGISTRY

from hospitality.ai.gateway.api import refresh_call_outcome_metrics
from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.metrics import LlmCallOutcomes, set_llm_call_outcomes
from hospitality.shared.tenancy import tenant_context

pytestmark = pytest.mark.usefixtures("canonical_database")

OK = LlmCallStatus.OK
ERROR = LlmCallStatus.ERROR
TIMEOUT = LlmCallStatus.TIMEOUT
BUDGET = LlmCallStatus.BUDGET_EXCEEDED


async def _write_calls(tenant_id: uuid.UUID, statuses: list[LlmCallStatus]) -> None:
    """Журнал тенанта: статусы по порядку, с шагом в минуту до текущего момента."""
    now = utc_now()
    await _write_calls_at(
        tenant_id,
        [
            (status, now - timedelta(minutes=len(statuses) - index))
            for index, status in enumerate(statuses)
        ],
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


def _outcomes(tenant_id: uuid.UUID) -> LlmCallOutcomes | None:
    labels = {"tenant_id": str(tenant_id)}
    errors = REGISTRY.get_sample_value("llm_provider_failure_streak", labels | {"status": "error"})
    timeouts = REGISTRY.get_sample_value(
        "llm_provider_failure_streak", labels | {"status": "timeout"}
    )
    rejections = REGISTRY.get_sample_value("llm_budget_rejection_streak", labels)
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
    day_start = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
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
    set_llm_call_outcomes(
        {
            str(stale_tenant): LlmCallOutcomes(
                provider_errors=5, provider_timeouts=0, budget_rejections=0
            )
        }
    )

    def explode() -> None:
        raise RuntimeError("postgres is down")

    monkeypatch.setattr("hospitality.ai.gateway.outcomes.platform_session_scope", explode)
    await refresh_call_outcome_metrics()

    assert _outcomes(stale_tenant) is None
