"""Дневной номер заявки (#N) — заход 2а по issue #38.

Решение основателя (2026-07-17): человеческий номер `#12`, сброс раз в сутки по
времени отеля; уникальность в паре `(тенант, день по tz отеля, daily_number)`.
Номер — метка для глаз/речи/отчёта, не ключ действия: разные дни могут повторять
`#12`. Здесь проверяется присвоение, per-tenant/day уникальность, защита от гонки
и резолв незакрытой заявки по номеру.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.requests import api as requests_api
from hospitality.modules.requests import service
from hospitality.modules.requests.models import ServiceRequest
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context

from .conftest import make_category, two_tenants  # noqa: F401 (фикстура для pytest)


async def _create(tenant_id: uuid.UUID, category_id: uuid.UUID, summary: str) -> int:
    with tenant_context(tenant_id):
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(category_id=category_id, summary=summary)
        )
    assert request.daily_number is not None  # новая заявка всегда получает номер
    return request.daily_number


async def test_daily_number_starts_at_one_and_increments(
    two_tenants: tuple[uuid.UUID, uuid.UUID],  # noqa: F811
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    first = await _create(tenant_a, category.id, "полотенца 305")
    second = await _create(tenant_a, category.id, "лампочка 210")

    assert first == 1
    assert second == 2


async def test_daily_number_is_per_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],  # noqa: F811
) -> None:
    """Номер уникален в паре (тенант, день): у каждого отеля свой отсчёт с 1."""
    tenant_a, tenant_b = two_tenants
    category_a = await make_category(tenant_a)
    with tenant_context(tenant_b):
        category_b = await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
        )

    await _create(tenant_a, category_a.id, "заявка A1")
    await _create(tenant_a, category_a.id, "заявка A2")
    first_b = await _create(tenant_b, category_b.id, "заявка B1")

    assert first_b == 1  # чужие заявки на счётчик тенанта B не влияют (RLS, P-4)


async def test_daily_number_retries_on_race(
    two_tenants: tuple[uuid.UUID, uuid.UUID],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Гонка: два процесса прочитали один max и претендуют на один номер.

    Уникальный индекс (tenant_id, service_day, daily_number) отвергает второй
    INSERT, `create_request` пересчитывает номер и повторяет — дубликата нет.
    Эмулируем устаревшее чтение: первый вызов `_next_daily_number` возвращает
    уже занятый номер.
    """
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    first = await _create(tenant_a, category.id, "первая")
    assert first == 1

    original_next = service._next_daily_number
    calls = {"n": 0}

    async def stale_then_real(session: AsyncSession, service_day: date) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return 1  # устаревшее чтение: номер уже занят первой заявкой
        return await original_next(session, service_day)

    monkeypatch.setattr(service, "_next_daily_number", stale_then_real)

    second = await _create(tenant_a, category.id, "вторая")

    assert second == 2  # не 1 — коллизия отвергнута, номер пересчитан
    assert calls["n"] == 2  # был ровно один повтор


async def test_find_open_request_by_daily_number(
    two_tenants: tuple[uuid.UUID, uuid.UUID],  # noqa: F811
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(category_id=category.id, summary="номер один")
        )
        assert created.daily_number is not None
        matches = await requests_api.find_open_requests_by_daily_number(created.daily_number)

    assert [m.id for m in matches] == [created.id]


async def test_same_number_across_days_yields_multiple_candidates(
    two_tenants: tuple[uuid.UUID, uuid.UUID],  # noqa: F811
) -> None:
    """Незакрытая заявка вчерашнего дня делит `#N` с сегодняшней → два кандидата.

    Ради этого сценария и существует резолв с уточнением (issue #38: номер —
    метка, не ключ). «Вчерашний день» эмулируем сдвигом `service_day` первой
    заявки на день назад — после чего сегодняшний счётчик снова стартует с #1.
    """
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        yesterday = await requests_api.create_request(
            requests_api.ServiceRequestCreate(category_id=category.id, summary="вчерашняя")
        )
        # Сдвигаем день первой заявки назад: она перестаёт влиять на сегодняшний
        # max, и следующая заявка снова получает #1 (тот же номер, другой день).
        async with session_scope() as session:
            row = (
                await session.execute(
                    select(ServiceRequest).where(ServiceRequest.id == yesterday.id)
                )
            ).scalar_one()
            assert row.service_day is not None
            row.service_day = row.service_day - timedelta(days=1)
        today = await requests_api.create_request(
            requests_api.ServiceRequestCreate(category_id=category.id, summary="сегодняшняя")
        )
        matches = await requests_api.find_open_requests_by_daily_number(1)

    assert today.daily_number == 1
    assert {m.id for m in matches} == {yesterday.id, today.id}


async def test_closed_request_is_not_resolved_by_number(
    two_tenants: tuple[uuid.UUID, uuid.UUID],  # noqa: F811
) -> None:
    """Резолв идёт только среди незакрытых: завершённая заявка по номеру не находится."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        created = await requests_api.create_request(
            requests_api.ServiceRequestCreate(category_id=category.id, summary="закрыть")
        )
        await requests_api.change_request_status(created.id, requests_api.RequestStatus.IN_PROGRESS)
        await requests_api.change_request_status(created.id, requests_api.RequestStatus.DONE)
        assert created.daily_number is not None
        matches = await requests_api.find_open_requests_by_daily_number(created.daily_number)

    assert matches == []
