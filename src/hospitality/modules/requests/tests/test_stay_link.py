"""Заявка знает своё проживание (spec 0054, issue #228).

`stay_id` — аргумент `create_request`, а не поле схемы: пишет его только код с
проверенным Stay. Что HTTP-тело его не пишет — `test_http_api.py`; что ретеншн
его снимает — `channels/common/tests/test_retention.py`.
"""

from __future__ import annotations

import uuid

from hospitality.modules.requests.api import (
    ServiceRequestCreate,
    ServiceRequestOrigin,
    create_request,
    get_request,
)
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.tenancy import tenant_context


async def test_request_keeps_the_stay_it_was_created_for(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Две заявки из одной комнаты от двух проживаний различимы по `stay_id` —
    ради этого колонка и заведена (одна комната за сутки — два гостя)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    yesterday_stay, today_stay = uuid.uuid4(), uuid.uuid4()

    with tenant_context(tenant_a):
        yesterday = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="полотенца",
                room_number="704",
            ),
            stay_id=yesterday_stay,
        )
        today = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="полотенца",
                room_number="704",
            ),
            stay_id=today_stay,
        )
        stored = await get_request(yesterday.id)

    assert yesterday.stay_id == yesterday_stay
    assert today.stay_id == today_stay
    assert stored.stay_id == yesterday_stay


async def test_request_without_verified_stay_has_none(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Кабинет, Telegram и HTTP API Stay не знают — пусто, а не догадка по комнате."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        created = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.STAFF_MANUAL,
                summary="перегорела лампа",
                room_number="704",
            )
        )

    assert created.stay_id is None
