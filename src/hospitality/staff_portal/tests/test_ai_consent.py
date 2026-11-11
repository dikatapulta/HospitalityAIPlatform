"""Отметка согласия сотрудника на ИИ-разделы (spec 0082, issue #985).

Без отметки получателя раздел закрыт: пункта меню нет, страница — «Раздел закрыт» (403),
действия — 403 ERR-AUTH-003 без вызова модели. Ставит и снимает менеджер в «Сотрудниках»;
суперадмину отметка не нужна. Помощники и фикстуры «Наставника» — из `test_mentor.py`
(канон раздела), «Предложить идею» — из `test_ideas.py`.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid

from httpx import AsyncClient, Response
from sqlalchemy import select
from structlog.testing import capture_logs

from hospitality.ai.gateway.api import MockEmbeddingProvider, MockLlmProvider
from hospitality.ai.mentor.common import MentorProviders
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import AiRecipient, StaffRole, Tenant, TenantMembership
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import ai_consent
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from hospitality.staff_portal.tests.test_ideas import _ideas_model  # noqa: F401
from hospitality.staff_portal.tests.test_mentor import (  # noqa: F401  (фикстуры-реимпорт)
    API,
    HOME,
    PAGE,
    SAME_ORIGIN,
    Portal,
    _hotel,
    _mentor_models,
    _post,
    portal,
)
from tests.test_staff_auth import create_staff_user, unique_login
from tests.test_staff_superadmin import create_superadmin

IDEAS_PAGE = f"{HOME}/ideas"
IDEAS_API = f"{HOME}/api/ideas"
TEAM_PAGE = f"{HOME}/team"
TEAM_API = f"{HOME}/api/team"


async def _staff(
    client: AsyncClient, hotel: PortalHotel, *consents: AiRecipient, login: bool = True
) -> uuid.UUID:
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login,
        tenant_id=hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Әсел",
        ai_consents=consents,
    )
    if login:
        await submit_login(client, staff_login)
    return user_id


def _watch(portal: Portal) -> MockLlmProvider:
    """Fake-адаптеры шлюза на один `MockLlmProvider`: его `calls` — был ли вызов модели."""
    model = MockLlmProvider(text="Ответ.")
    portal.answer_with(
        MentorProviders(chat=model, streaming=model, embeddings=MockEmbeddingProvider())
    )
    return model


async def _consents(user_id: uuid.UUID, tenant_id: uuid.UUID) -> list[str]:
    async with platform_session_scope() as session:
        stored = await session.scalar(
            select(TenantMembership.ai_consents).where(
                TenantMembership.user_id == user_id, TenantMembership.tenant_id == tenant_id
            )
        )
    assert stored is not None
    return sorted(stored)


async def _set(client: AsyncClient, user_id: uuid.UUID, *recipients: str) -> Response:
    return await client.post(
        f"{TEAM_API}/members/{user_id}/ai-consent",
        json={"recipients": list(recipients)},
        headers=SAME_ORIGIN,
    )


async def _config(
    tenant_id: uuid.UUID, *, mentor: bool = False, ideas: bool = False, memos: bool = False
) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                mentor_enabled=mentor,
                ideas_enabled=ideas,
                memos_enabled=memos,
            ),
        )


async def test_mentor_is_closed_without_openai_mark(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    """Отметка другого получателя не открывает: «Наставник» — только с `openai`."""
    await _hotel(portal_hotel.tenant_id)
    await _staff(portal.client, portal_hotel, AiRecipient.ANTHROPIC)
    model = _watch(portal)

    assert f'href="{PAGE}"' not in (await portal.client.get(HOME)).text
    page = await portal.client.get(PAGE)
    assert page.status_code == 403
    assert "Раздел закрыт" in page.text and "OpenAI (США)" in page.text
    assert (await portal.client.get(f"{PAGE}/codex")).status_code == 403

    with capture_logs() as logs:
        asked = await _post(portal.client, "/ask", {"message": "Как встретить гостя?"})
    assert asked.status_code == 403
    assert asked.json()["error"]["code"] == "ERR-AUTH-003"
    assert model.calls == []
    missing = [log for log in logs if log["event"] == "staff.ai_consent_missing"]
    assert missing and missing[0]["recipient"] == "openai" and missing[0]["section"] == "mentor"

    assert (await _post(portal.client, "/exam/start", {})).status_code == 403
    flag = await portal.client.post(
        f"{API}/flags", json={"article_number": "2", "flagged": True}, headers=SAME_ORIGIN
    )
    assert flag.status_code == 403


async def test_disabled_section_stays_404_without_mark(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    """Сначала флаг отеля, потом отметка: выключенный раздел — 404, как неизвестный адрес."""
    await _hotel(portal_hotel.tenant_id, enabled=False, codex=False)
    await _staff(portal.client, portal_hotel)
    assert (await portal.client.get(PAGE)).status_code == 404


async def test_manager_mark_opens_and_clears_on_next_request(
    portal: Portal, portal_hotel: PortalHotel, second_client: AsyncClient
) -> None:
    await _hotel(portal_hotel.tenant_id)
    staff_id = await _staff(portal.client, portal_hotel)
    await submit_login(second_client, portal_hotel.login)
    assert (await portal.client.get(PAGE)).status_code == 403

    with capture_logs() as logs:
        assert (await _set(second_client, staff_id, "openai")).status_code == 200
    assert await _consents(staff_id, portal_hotel.tenant_id) == ["openai"]
    changed = [log for log in logs if log["event"] == "staff.member_ai_consents_changed"]
    assert changed[0]["previous_ai_consents"] == [] and changed[0]["ai_consents"] == ["openai"]
    assert (await portal.client.get(PAGE)).status_code == 200
    assert f'href="{PAGE}"' in (await portal.client.get(HOME)).text

    with capture_logs() as logs:
        assert (await _set(second_client, staff_id, "openai")).status_code == 200
    assert not [log for log in logs if log["event"] == "staff.member_ai_consents_changed"]

    assert (await _set(second_client, staff_id)).status_code == 200
    assert await _consents(staff_id, portal_hotel.tenant_id) == []
    assert (await portal.client.get(PAGE)).status_code == 403


async def test_ideas_need_anthropic_mark(portal: Portal, portal_hotel: PortalHotel) -> None:
    await _config(portal_hotel.tenant_id, ideas=True)
    await _staff(portal.client, portal_hotel, AiRecipient.OPENAI)
    model = _watch(portal)

    assert f'href="{IDEAS_PAGE}"' not in (await portal.client.get(HOME)).text
    page = await portal.client.get(IDEAS_PAGE)
    assert page.status_code == 403 and "Anthropic (США)" in page.text
    turn = await portal.client.post(
        f"{IDEAS_API}/turn", json={"mode": "idea", "message": "Идея"}, headers=SAME_ORIGIN
    )
    assert turn.status_code == 403 and model.calls == []
    assert (await portal.client.get(f"{IDEAS_PAGE}/mine")).status_code == 403


async def test_superadmin_needs_no_mark(portal_hotel: PortalHotel) -> None:
    """Суперадмин — сам оператор, не работник отеля (spec 0082 §2.2)."""

    def context(*, is_platform_admin: bool) -> StaffContext:
        return StaffContext(
            session_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            display_name="Основатель",
            is_platform_admin=is_platform_admin,
            tenant_id=portal_hotel.tenant_id,
            tenant_slug=HOTEL_SLUG,
            tenant_name="Отель",
            role_key=StaffRole.MANAGER,
        )

    superadmin = context(is_platform_admin=True)
    manager = context(is_platform_admin=False)
    for section in ai_consent.AiSection:
        assert ai_consent.has(superadmin, section)
        assert not ai_consent.has(manager, section)
    assert AiRecipient.known(["openai", "someday"]) == {AiRecipient.OPENAI}


async def test_consent_action_rights(
    client: AsyncClient, second_client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Себе — можно; суперадмину — 409; чужой отель — 404; не менеджер — 403; без
    CSRF — 403; неизвестный получатель — 422."""
    await submit_login(client, portal_hotel.login)
    assert (await _set(client, portal_hotel.user_id, "openai")).status_code == 200
    assert await _consents(portal_hotel.user_id, portal_hotel.tenant_id) == ["openai"]

    superadmin_id, _ = await create_superadmin(portal_hotel.tenant_id, unique_login())
    refused = await _set(client, superadmin_id, "openai")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "ERR-AUTH-013"

    async with platform_session_scope() as session:
        other = Tenant(slug="other-hotel", name="Другой")
        session.add(other)
        await session.flush()
        other_id = other.id
    stranger = await create_staff_user(unique_login(), tenant_id=other_id, role=StaffRole.STAFF)
    missing = await _set(client, stranger, "openai")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "ERR-AUTH-008"

    assert (await _set(client, portal_hotel.user_id, "google")).status_code == 422
    no_origin = await client.post(
        f"{TEAM_API}/members/{portal_hotel.user_id}/ai-consent", json={"recipients": []}
    )
    assert no_origin.status_code == 403

    staff_id = await _staff(second_client, portal_hotel)
    assert (await _set(second_client, staff_id, "openai")).status_code == 403


async def test_team_shows_marks_only_for_enabled_sections(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    staff_id = await _staff(client, portal_hotel, AiRecipient.OPENAI, login=False)
    await submit_login(client, portal_hotel.login)
    await _config(portal_hotel.tenant_id)
    page = (await client.get(TEAM_PAGE)).text
    assert "data-ai-consent" not in page and ai_consent.TEAM_LEGEND not in page

    await _config(portal_hotel.tenant_id, mentor=True)
    page = (await client.get(TEAM_PAGE)).text
    assert f'data-ai-consent data-user-id="{staff_id}"' in page
    assert ai_consent.RECIPIENT_CHIPS[AiRecipient.OPENAI] in page
    assert 'value="anthropic"' not in page
    assert ai_consent.TEAM_HINT in page

    await _config(portal_hotel.tenant_id, mentor=True, memos=True)
    assert 'value="anthropic"' in (await client.get(TEAM_PAGE)).text


async def test_results_leave_out_staff_without_openai_mark(
    portal: Portal, portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    await _staff(portal.client, portal_hotel, AiRecipient.OPENAI, login=False)
    await create_staff_user(
        unique_login(),
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Без Согласия",
        ai_consents=(),
    )
    with tenant_context(portal_hotel.tenant_id):
        await mentor_api.create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=None
        )
    await submit_login(portal.client, portal_hotel.login)
    page = (await portal.client.get(f"{PAGE}/results")).text
    note = ai_consent.results_note(1)
    assert "Без Согласия" not in page and "Әсел" in page
    assert note is not None and note in page
    assert note == (
        "Без отметки согласия на «Наставника» — 1 сотрудник: экзамен им не виден, в итогах их нет."
    )
