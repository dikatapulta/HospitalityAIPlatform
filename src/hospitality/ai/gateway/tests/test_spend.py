"""Расход на модель за окно — `spend_usd_between` (issue #301, spec 0035 §6).

Число строки «ИИ за сутки отеля: $1.84» в копии сводки основателю. Проверяется
то, за что оно отвечает: границы окна (полуоткрытые) и изоляция тенантов —
считать чужие доллары нельзя даже случайно.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from hospitality.ai.gateway import api as gateway
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context


async def _log_call(tenant_id: uuid.UUID, *, cost_usd: str, created_at: datetime) -> None:
    """Строка журнала вызовов — SQL'ом: стоимость считает сам gateway по прайсу,
    а тесту нужен ровно момент и ровно сумма."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO llm_call_log (id, tenant_id, provider, model, prompt_hash, "
                    "status, input_tokens, output_tokens, cost_usd, latency_ms, created_at) "
                    "VALUES (:id, :tenant_id, 'anthropic', 'claude-sonnet-5', 'hash', 'ok', "
                    "10, 20, :cost_usd, 100, :created_at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "cost_usd": Decimal(cost_usd),
                    "created_at": created_at,
                },
            )


async def test_spend_sums_only_the_window(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Окно полуоткрытое `[created_after, created_before)`.

    Вызов ровно на верхней границе принадлежит СЛЕДУЮЩЕМУ дню: иначе полночный
    вызов попал бы в сумму дважды — и вчера, и сегодня.
    """
    demo_tenant, _ = two_tenants
    day_start = utc_now().replace(microsecond=0) - timedelta(hours=1)
    day_end = day_start + timedelta(hours=1)
    await _log_call(demo_tenant, cost_usd="1.50", created_at=day_start)
    await _log_call(demo_tenant, cost_usd="0.34", created_at=day_start + timedelta(minutes=30))
    await _log_call(demo_tenant, cost_usd="9.99", created_at=day_end)
    await _log_call(demo_tenant, cost_usd="7.77", created_at=day_start - timedelta(seconds=1))

    with tenant_context(demo_tenant):
        spent = await gateway.spend_usd_between(created_after=day_start, created_before=day_end)
    assert spent == Decimal("1.84")


async def test_spend_without_calls_is_zero(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Вызовов не было — ноль долларов, а не `None`: «расход неизвестен» и
    «расхода не было» для сводки разные вещи, и здесь именно второе."""
    demo_tenant, _ = two_tenants
    now = utc_now()
    with tenant_context(demo_tenant):
        spent = await gateway.spend_usd_between(
            created_after=now - timedelta(days=1), created_before=now
        )
    assert spent == Decimal(0)


async def test_spend_is_isolated_per_tenant(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """P-4: чужой расход отсекает RLS, а не фильтр в запросе."""
    first, second = two_tenants
    now = utc_now().replace(microsecond=0)
    await _log_call(first, cost_usd="2.00", created_at=now - timedelta(minutes=5))
    await _log_call(second, cost_usd="5.00", created_at=now - timedelta(minutes=5))

    window = {"created_after": now - timedelta(hours=1), "created_before": now}
    with tenant_context(first):
        assert await gateway.spend_usd_between(**window) == Decimal("2.00")
    with tenant_context(second):
        assert await gateway.spend_usd_between(**window) == Decimal("5.00")
