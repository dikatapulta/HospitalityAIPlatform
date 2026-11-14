"""Сброс пароля менеджером отеля (#944, spec 0044 §4): сотруднику и старшему —
сам, менеджеру — только суперадмин; уведомление основателю тем же путём."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select, update

from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.events import SuperadminActionKind
from hospitality.platform.models import (
    MembershipStatus,
    StaffPasswordReset,
    StaffRole,
    Tenant,
    TenantMembership,
)
from hospitality.platform.staff_auth import ERR_AUTH_FORBIDDEN
from hospitality.platform.staff_password_policy import ERR_AUTH_PASSWORD_GUESSABLE
from hospitality.platform.staff_password_reset import (
    accept_password_reset,
    password_set_by_link_at,
)
from hospitality.platform.staff_push import StaffPushSubscription
from hospitality.platform.staff_superadmin import issue_password_reset
from hospitality.platform.staff_team import ERR_AUTH_SELF_ACTION, ERR_AUTH_SUPERADMIN_TARGET
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis
from tests.test_logging import find_record, read_log_records
from tests.test_staff_auth import RECEPTION, create_staff_user, unique_login
from tests.test_staff_passkeys import _phone
from tests.test_staff_superadmin import NEW_PASSWORD, create_superadmin, outbox_kinds


@pytest.fixture(autouse=True)
def _counting_rate_limit() -> Iterator[FakeRateLimitRedis]:
    """Свой MonkeyPatch, как в `test_staff_superadmin.py` (довод там)."""
    patcher = pytest.MonkeyPatch()
    fake_redis = FakeRateLimitRedis()
    patcher.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    yield fake_redis
    patcher.undo()


@pytest.fixture
async def tenant(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug="hotel-m", name="Hotel M")
        session.add(row)
        await session.flush()
        return row


async def _member(tenant: Tenant, role: StaffRole) -> tuple[uuid.UUID, str]:
    staff_login = unique_login()
    service_keys = () if role is StaffRole.MANAGER else RECEPTION
    member_id = await create_staff_user(
        staff_login, tenant_id=tenant.id, role=role, service_keys=service_keys
    )
    return member_id, staff_login


@pytest.mark.parametrize("role", [StaffRole.STAFF, StaffRole.SENIOR])
async def test_manager_resets_staff_and_senior(
    tenant: Tenant, role: StaffRole, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging()
    manager_id, _ = await _member(tenant, StaffRole.MANAGER)
    member_id, member_login = await _member(tenant, role)

    reset = await issue_password_reset(member_id, tenant.id, actor_user_id=manager_id)

    assert reset.login == member_login
    # Кто и кому — строка ссылки и лог; основателю — своё уведомление.
    async with platform_session_scope() as session:
        row = await session.scalar(select(StaffPasswordReset))
    assert row is not None
    assert row.issued_by == manager_id
    assert await outbox_kinds() == [SuperadminActionKind.MANAGER_PASSWORD_RESET_ISSUED.value]
    records = read_log_records(capsys)
    record = find_record(records, "staff.password_reset_issued")
    assert record["actor_user_id"] == str(manager_id)
    assert record["user_id"] == str(member_id)
    assert record["by_superadmin"] is False
    assert member_login not in str(records)  # логин — ПД, в логи не пишется

    # Ссылка та же, что у суперадмина: по ней задаётся пароль, и время сброса
    # видно подписи паролем (spec 0077 §4).
    assert await password_set_by_link_at(member_id, tenant.id, member_login) is None
    await accept_password_reset(reset.reset_token, password=NEW_PASSWORD)
    assert await password_set_by_link_at(member_id, tenant.id, member_login) is not None


async def test_city_of_hotel_in_new_password_rejected_and_link_kept(tenant: Tenant) -> None:
    """Дверь нового пароля передаёт политике слова отеля (#967): город из конфига
    тенанта латиницей — отказ, ссылка жива и принимает другой пароль."""
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant.id,
            TenantConfig(
                profile=HotelProfile(city="Талдыкорган", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
            ),
        )
    manager_id, _ = await _member(tenant, StaffRole.MANAGER)
    member_id, _ = await _member(tenant, StaffRole.STAFF)
    reset = await issue_password_reset(member_id, tenant.id, actor_user_id=manager_id)

    with pytest.raises(AppError) as error:
        await accept_password_reset(reset.reset_token, password="Taldykorgan-2026")
    assert error.value.code == ERR_AUTH_PASSWORD_GUESSABLE
    await accept_password_reset(reset.reset_token, password=NEW_PASSWORD)


async def test_manager_cannot_reset_manager_self_or_superadmin(tenant: Tenant) -> None:
    manager_id, _ = await _member(tenant, StaffRole.MANAGER)
    other_manager_id, _ = await _member(tenant, StaffRole.MANAGER)
    admin_id, _ = await create_superadmin(tenant.id, unique_login())

    with pytest.raises(AppError) as manager_error:
        await issue_password_reset(other_manager_id, tenant.id, actor_user_id=manager_id)
    assert manager_error.value.code == ERR_AUTH_FORBIDDEN
    with pytest.raises(AppError) as self_error:
        await issue_password_reset(manager_id, tenant.id, actor_user_id=manager_id)
    assert self_error.value.code == ERR_AUTH_SELF_ACTION
    with pytest.raises(AppError) as admin_error:
        await issue_password_reset(admin_id, tenant.id, actor_user_id=manager_id)
    assert admin_error.value.code == ERR_AUTH_SUPERADMIN_TARGET
    assert await outbox_kinds() == []


async def test_superadmin_still_resets_manager(tenant: Tenant) -> None:
    admin_id, _ = await create_superadmin(tenant.id, unique_login())
    manager_id, _ = await _member(tenant, StaffRole.MANAGER)
    await issue_password_reset(manager_id, tenant.id, actor_user_id=admin_id)
    assert await outbox_kinds() == [SuperadminActionKind.PASSWORD_RESET_ISSUED.value]


async def test_manager_of_another_hotel_or_revoked_cannot_reset(tenant: Tenant) -> None:
    member_id, _ = await _member(tenant, StaffRole.STAFF)
    async with platform_session_scope() as session:
        foreign = Tenant(slug="hotel-m2", name="Hotel M2")
        session.add(foreign)
        await session.flush()
        foreign_id = foreign.id
    foreign_manager_id = await create_staff_user(
        unique_login(), tenant_id=foreign_id, role=StaffRole.MANAGER
    )
    with pytest.raises(AppError) as foreign_error:
        await issue_password_reset(member_id, tenant.id, actor_user_id=foreign_manager_id)
    assert foreign_error.value.code == ERR_AUTH_FORBIDDEN

    # Членство менеджера отозвано между страницей и действием — повторная
    # проверка в транзакции ловит это.
    manager_id, _ = await _member(tenant, StaffRole.MANAGER)
    async with platform_session_scope() as session:
        await session.execute(
            update(TenantMembership)
            .where(TenantMembership.user_id == manager_id)
            .values(status=MembershipStatus.REVOKED)
        )
    with pytest.raises(AppError) as revoked_error:
        await issue_password_reset(member_id, tenant.id, actor_user_id=manager_id)
    assert revoked_error.value.code == ERR_AUTH_FORBIDDEN


async def test_new_password_deletes_push_subscriptions_of_the_account(tenant: Tenant) -> None:
    """#1136: телефон, где под учёткой включили «Уведомления» со старым паролем,
    после нового пароля её пушей не получает; подписки коллег на месте. Свой
    телефон кабинет подпишет заново при входе с новым паролем (`push.js`)."""
    manager_id, _ = await _member(tenant, StaffRole.MANAGER)
    member_id, _ = await _member(tenant, StaffRole.STAFF)
    await _phone(tenant.id, member_id)
    await _phone(tenant.id, member_id)
    colleague_phone = await _phone(tenant.id, manager_id)
    reset = await issue_password_reset(member_id, tenant.id, actor_user_id=manager_id)

    await accept_password_reset(reset.reset_token, password=NEW_PASSWORD)

    with tenant_context(tenant.id):
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(StaffPushSubscription.user_id, StaffPushSubscription.endpoint)
                )
            ).all()
    assert [tuple(row) for row in rows] == [(manager_id, colleague_phone)]
