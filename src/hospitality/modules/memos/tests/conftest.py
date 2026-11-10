"""Фикстуры тестов модуля memos (spec 0077 §12, PR 3) — канон
`modules/feedback/tests/conftest.py`: общие DB-фикстуры реимпортируются из
`tests/conftest.py`.

Отель: автор, два подписанта, адресат-менеджер, старший, менеджер-неучастник и
посторонний — у каждого свой вход (кабинет с «Запомнить меня»), как у страницы.
Подтверждают паролем кабинета; ключ устройства — `SoftAuthenticator` из тестов PR 2.

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from hospitality.modules.memos import api as memos_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_passkey_binding import begin_binding, finish_binding
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    FakeRateLimitRedis,
    _clean_log_context,
    _frozen_ratelimit_clock,
    _isolated_event_subscribers,
    canonical_database,
    fake_file_storage,
    migrated_database_name,
)
from tests.passkey_authenticator import IPHONE_SAFARI_UA, SoftAuthenticator, staff_member
from tests.test_staff_auth import PASSWORD

IP = "ip-memos"
TITLE = "Закупка полотенец"
BODY = "Прошу закупить: 40 банных полотенец. Для чего: замена износившихся."
STROKES = memos_api.SignatureStrokes(strokes=(((10, 10), (200, 180), (400, 90)),))


@dataclass(frozen=True)
class Hotel:
    tenant_id: uuid.UUID
    slug: str
    author: StaffContext
    signer_a: StaffContext
    signer_b: StaffContext
    boss: StaffContext  # адресат, manager
    senior: StaffContext
    other_manager: StaffContext
    outsider: StaffContext
    author_device: SoftAuthenticator


@pytest.fixture
def fake_redis() -> Iterator[FakeRateLimitRedis]:
    fake = FakeRateLimitRedis()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
        yield fake


async def configure(tenant_id: uuid.UUID, **fields: object) -> TenantConfig:
    config = TenantConfig.model_validate(
        {
            "profile": HotelProfile(city="Almaty", country_code="KZ"),
            "timezone": "Asia/Almaty",
            "default_language": "ru",
            "memos_enabled": True,
            **fields,
        }
    )
    async with platform_session_scope() as session:
        await store_tenant_config(session, tenant_id, config)
    return config


async def make_hotel(slug: str) -> Hotel:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name=slug)
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id
    await configure(tenant_id)

    async def member(role: StaffRole = StaffRole.STAFF) -> StaffContext:
        return (await staff_member(tenant_id, slug, role=role)).context

    author = await member()
    device = SoftAuthenticator()
    options = await begin_binding(author, PASSWORD, client_ip=IP, device_token=None)
    await finish_binding(
        author, device.register(options), user_agent=IPHONE_SAFARI_UA, notice_url="/"
    )
    hotel = Hotel(
        tenant_id=tenant_id,
        slug=slug,
        author=author,
        signer_a=await member(),
        signer_b=await member(),
        boss=await member(StaffRole.MANAGER),
        senior=await member(StaffRole.SENIOR),
        other_manager=await member(StaffRole.MANAGER),
        outsider=await member(),
        author_device=device,
    )
    with tenant_context(tenant_id):
        for staff in (hotel.author, hotel.signer_a, hotel.signer_b, hotel.boss):
            await memos_api.set_signature_sample(staff, STROKES)
    return hotel


@pytest.fixture
async def hotel(
    canonical_database: None,
    fake_redis: FakeRateLimitRedis,
    fake_file_storage: object,
) -> Hotel:
    return await make_hotel("hotel-memos")


def draft(
    hotel: Hotel, *, signers: tuple[StaffContext, ...] | None = None
) -> memos_api.MemoDraftInput:
    chosen = (hotel.signer_a, hotel.signer_b) if signers is None else signers
    return memos_api.MemoDraftInput(
        template_key="purchase",
        title=TITLE,
        body=BODY,
        addressee_id=hotel.boss.user_id,
        signer_ids=tuple(staff.user_id for staff in chosen),
    )


async def act(
    hotel: Hotel,
    staff: StaffContext,
    memo_id: uuid.UUID,
    intent: memos_api.MemoIntent,
    *,
    device: SoftAuthenticator | None = None,
) -> memos_api.MemoStatus:
    """Окно подтверждения целиком: ключом устройства, если он дан, иначе паролем."""
    with tenant_context(hotel.tenant_id):
        request = await memos_api.begin_action(staff, memo_id, intent)
        answer: memos_api.PasskeyAnswer | memos_api.PasswordAnswer
        if device is not None:
            answer = memos_api.PasskeyAnswer(
                challenge_id=request.challenge_id,
                credential_json=device.sign(request.options_json),
            )
        else:
            answer = memos_api.PasswordAnswer(
                challenge_id=request.challenge_id, password=PASSWORD, client_ip=IP
            )
        return await memos_api.complete_action(staff, memo_id, intent, answer)


async def sent_memo(hotel: Hotel, *, signers: tuple[StaffContext, ...] | None = None) -> uuid.UUID:
    """Записка, отправленная автором с подтверждением ключом устройства."""
    with tenant_context(hotel.tenant_id):
        memo_id = await memos_api.create_draft(hotel.author, draft(hotel, signers=signers))
    await act(hotel, hotel.author, memo_id, memos_api.SendIntent(), device=hotel.author_device)
    return memo_id
