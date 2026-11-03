"""Демо-заполнение задаёт службу эскалаций — ресепшен (#785, `tools/demo_showcase`).

Без службы эскалаций «позовите человека» и «сменить номер» приходят только
в Telegram, а карточки в кабинете нет (spec 0039 §2.3). Отдельный файл: картина
целиком и её защиты — `tests/test_demo_showcase_tool.py`.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.channels.common.escalation_request import escalate
from hospitality.channels.common.store import ensure_conversation
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig, load_tenant_config, mutate_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import find_hotel
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_showcase import ESCALATION_LINE, build_showcase
from hospitality.tools.seed import seed_demo_data
from tests.test_demo_showcase_tool import STAGING_ENV, _set_env
from tests.test_staff_auth import create_staff_user


@pytest.fixture
async def showcase_tenant(canonical_database: None, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Демо-отель сида с менеджером и окружение staging — как у тестов картины."""
    _set_env(monkeypatch, STAGING_ENV)
    await seed_demo_data()
    hotel = await find_hotel("demo-hotel")
    assert hotel is not None
    await create_staff_user(
        "BOSS", tenant_id=hotel.tenant_id, role=StaffRole.MANAGER, display_name="Менеджер Демо"
    )
    return hotel.tenant_id


async def _config(tenant_id: uuid.UUID) -> TenantConfig:
    async with platform_session_scope() as session:
        return await load_tenant_config(session, tenant_id)


async def _reception_requests(tenant_id: uuid.UUID) -> list[requests_api.ServiceRequestRead]:
    with tenant_context(tenant_id):
        categories = await requests_api.list_categories()
        reception = next(c.id for c in categories if c.key == RECEPTION_SERVICE_KEY)
        return await requests_api.list_open_requests(limit=100, category_ids=[reception])


async def test_fill_sets_reception_and_escalation_opens_a_card(showcase_tenant: uuid.UUID) -> None:
    assert (await _config(showcase_tenant)).escalation_category_key is None

    report = await build_showcase(confirmed=True, reset=True, invite=False)

    assert (await _config(showcase_tenant)).escalation_category_key == RECEPTION_SERVICE_KEY
    assert ESCALATION_LINE in report.lines
    before = {r.id for r in await _reception_requests(showcase_tenant)}
    with tenant_context(showcase_tenant):
        conversation_id = await ensure_conversation("web", "demo-guest")
        await escalate(
            conversation_id,
            uuid.uuid4(),
            chat_id="demo-guest",
            guest_message="хочу сменить номер",
            escalation=EscalationContext(
                reason=EscalationReason.GUEST_REQUESTED_STAFF, error_code="ERR-AI-004"
            ),
            verified_room_number="1405",
            verified_stay_id=None,
        )
    created = [r for r in await _reception_requests(showcase_tenant) if r.id not in before]
    assert len(created) == 1
    assert created[0].origin is requests_api.ServiceRequestOrigin.ESCALATION
    assert created[0].room_number == "1405"


async def test_refill_changes_nothing(showcase_tenant: uuid.UUID) -> None:
    await build_showcase(confirmed=True, reset=True, invite=False)
    first = await _config(showcase_tenant)

    report = await build_showcase(confirmed=True, reset=True, invite=False)

    assert await _config(showcase_tenant) == first
    assert ESCALATION_LINE in report.lines


async def test_keeps_escalation_service_set_before(showcase_tenant: uuid.UUID) -> None:
    async with platform_session_scope() as session:
        await mutate_tenant_config(
            session,
            showcase_tenant,
            lambda config: config.model_copy(update={"escalation_category_key": "maintenance"}),
        )

    report = await build_showcase(confirmed=True, reset=True, invite=False)

    assert (await _config(showcase_tenant)).escalation_category_key == "maintenance"
    assert ESCALATION_LINE not in report.lines
