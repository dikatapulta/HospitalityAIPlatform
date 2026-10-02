"""Приглашения сотрудников (spec 0033 §3.4, §10; spec 0037 §4, §6, §10):
одноразовость, истечение, отзыв, логин из приглашения — формат и занятость
при выпуске, гонка при принятии, мёртвые приглашения без логина. Принятие
всегда заводит нового User (ревизия ADR-008 27.09.2026).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from hospitality.platform.models import (
    StaffInvite,
    StaffRole,
    Tenant,
    TenantMembership,
    User,
    UserIdentity,
)
from hospitality.platform.staff_auth import deactivate_user, login
from hospitality.platform.staff_credentials import (
    ERR_AUTH_LOGIN_INVALID,
    ERR_AUTH_PASSWORD_TOO_SHORT,
    password_external_id,
)
from hospitality.platform.staff_invites import (
    ERR_AUTH_INVITE_INVALID,
    _hash_token,
    accept_invite,
    create_invite,
    describe_invite,
    list_pending_invites,
    revoke_invite,
)
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from tests.test_staff_auth import PASSWORD, _unique_ip, create_staff_user, unique_login


@pytest.fixture
async def hotel(canonical_database: None) -> tuple[Tenant, uuid.UUID]:
    """Тенант + менеджер, который приглашает."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-a", name="Hotel A")
        session.add(tenant)
        await session.flush()
    manager_id = await create_staff_user(
        unique_login(), tenant_id=tenant.id, role=StaffRole.MANAGER
    )
    return tenant, manager_id


async def _other_hotel() -> uuid.UUID:
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
        return other.id


async def _user_count() -> int:
    async with platform_session_scope() as session:
        return int(await session.scalar(select(func.count()).select_from(User)) or 0)


async def test_accept_creates_user_identity_membership(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    grant = await create_invite(
        tenant.id, StaffRole.RECEPTIONIST, "Аружан", " aruz ", invited_by=manager_id
    )
    assert grant.login == "ARUZ"  # нормализован при выпуске

    result = await accept_invite(grant.invite_token, password=PASSWORD)

    assert (result.role_key, result.login, result.tenant_id) == (
        StaffRole.RECEPTIONIST,
        "ARUZ",
        tenant.id,
    )
    # Принявший входит логином из приглашения и видит членство.
    session_grant = await login(tenant.id, "aruz", PASSWORD, client_ip=_unique_ip())
    assert session_grant.user_id == result.user_id
    assert session_grant.display_name == "Аружан"
    assert [m.role_key for m in session_grant.memberships] == [StaffRole.RECEPTIONIST]
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, grant.invite_id)
        assert invite is not None
        assert invite.accepted_at is not None
        assert invite.accepted_user_id == result.user_id
        identity = await session.scalar(
            select(UserIdentity).where(UserIdentity.user_id == result.user_id)
        )
        stored = (await session.scalars(select(StaffInvite.token_hash))).all()
    assert identity is not None
    assert identity.external_id == password_external_id(tenant.id, "ARUZ")
    assert grant.invite_token not in stored  # в БД — только хэш


async def test_invite_is_single_use(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    grant = await create_invite(tenant.id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager_id)
    await accept_invite(grant.invite_token, password=PASSWORD)

    with pytest.raises(AppError) as error:
        await accept_invite(grant.invite_token, password=PASSWORD)
    assert error.value.code == ERR_AUTH_INVITE_INVALID


async def test_expired_and_revoked_and_unknown_are_indistinguishable(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    tenant, manager_id = hotel
    expired = await create_invite(
        tenant.id, StaffRole.STAFF, "Ерлан", "ERLA", invited_by=manager_id
    )
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, expired.invite_id)
        assert invite is not None
        invite.expires_at = utc_now() - timedelta(seconds=1)
    revoked = await create_invite(
        tenant.id, StaffRole.STAFF, "Ерлан", "ERLA2", invited_by=manager_id
    )
    await revoke_invite(revoked.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)

    for token in (expired.invite_token, revoked.invite_token, "no-such-token"):
        with pytest.raises(AppError) as error:
            await accept_invite(token, password=PASSWORD)
        assert error.value.code == ERR_AUTH_INVITE_INVALID
        assert error.value.status_code == 410


async def test_revoke_accepted_invite_fails_and_revoke_is_idempotent(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    tenant, manager_id = hotel
    accepted = await create_invite(tenant.id, StaffRole.STAFF, "Али", "ALIA", invited_by=manager_id)
    await accept_invite(accepted.invite_token, password=PASSWORD)
    with pytest.raises(AppError) as error:
        await revoke_invite(accepted.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
    assert error.value.code == ERR_AUTH_INVITE_INVALID

    pending = await create_invite(tenant.id, StaffRole.STAFF, "Али", "ALIA2", invited_by=manager_id)
    await revoke_invite(pending.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
    await revoke_invite(pending.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)  # no-op


async def test_pending_list_and_describe_are_tenant_scoped(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    """Страница «Сотрудники» видит только свои ожидающие ссылки, а отзыв чужой
    неотличим от несуществующей (тенантная граница PR F)."""
    tenant, manager_id = hotel
    other_id = await _other_hotel()
    mine = await create_invite(
        tenant.id, StaffRole.RECEPTIONIST, "Наш", "NASH", invited_by=manager_id
    )
    foreign = await create_invite(other_id, StaffRole.STAFF, "Чужой", "NASH", invited_by=manager_id)

    pending = await list_pending_invites(tenant.id)
    assert [(item.invite_id, item.invited_name, item.login) for item in pending] == [
        (mine.invite_id, "Наш", "NASH")
    ]
    assert pending[0].role_key is StaffRole.RECEPTIONIST

    # Описание ссылки не требует тенанта — его задаёт сам токен; страница
    # принятия называет логин и код отеля (spec 0037 §4).
    described = await describe_invite(mine.invite_token)
    assert described is not None
    assert (
        described.tenant_name,
        described.tenant_slug,
        described.invited_name,
        described.login,
    ) == ("Hotel A", "hotel-a", "Наш", "NASH")
    assert await describe_invite("no-such-token") is None

    with pytest.raises(AppError) as error:
        await revoke_invite(foreign.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
    assert error.value.code == ERR_AUTH_INVITE_INVALID
    assert len(await list_pending_invites(other_id)) == 1  # чужая ссылка цела

    await revoke_invite(mine.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
    assert await list_pending_invites(tenant.id) == []
    assert await describe_invite(mine.invite_token) is None


@pytest.mark.parametrize("bad_login", ["ИЩКЬ", "2BORM", "ab", "A" * 13, ""])
async def test_invite_with_wrong_login_format_is_not_issued(
    hotel: tuple[Tenant, uuid.UUID], bad_login: str
) -> None:
    tenant, manager_id = hotel
    with pytest.raises(AppError) as error:
        await create_invite(tenant.id, StaffRole.STAFF, "Имя", bad_login, invited_by=manager_id)
    assert (error.value.code, error.value.status_code) == (ERR_AUTH_LOGIN_INVALID, 422)
    assert await list_pending_invites(tenant.id) == []


async def test_taken_login_is_not_issued(hotel: tuple[Tenant, uuid.UUID]) -> None:
    """Spec 0037 §4: логин занят учёткой отеля (в т.ч. отключённой — §2) или
    ожидающим приглашением → ERR-AUTH-012 (409), ссылка не выпускается."""
    tenant, manager_id = hotel
    await create_staff_user("ACTIVE", tenant_id=tenant.id, role=StaffRole.STAFF)
    gone_id = await create_staff_user("GONE", tenant_id=tenant.id, role=StaffRole.STAFF)
    await deactivate_user(gone_id, actor_user_id=manager_id)
    await create_invite(tenant.id, StaffRole.STAFF, "Ждёт", "WAITS", invited_by=manager_id)

    for taken in ("active", "GONE", " waits "):
        with pytest.raises(AppError) as error:
            await create_invite(tenant.id, StaffRole.STAFF, "Другой", taken, invited_by=manager_id)
        assert (error.value.code, error.value.status_code) == (ERR_AUTH_LOGIN_INVALID, 409)
    assert [item.login for item in await list_pending_invites(tenant.id)] == ["WAITS"]


async def test_login_is_free_in_other_hotel_and_after_revoke(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    """Уникальность — внутри отеля (§2): тот же логин в соседнем отеле
    свободен; отозванное или истёкшее приглашение логин отпускает."""
    tenant, manager_id = hotel
    await create_staff_user("BORM", tenant_id=tenant.id, role=StaffRole.STAFF)
    assert await create_invite(
        await _other_hotel(), StaffRole.STAFF, "Тёзка", "BORM", invited_by=manager_id
    )

    revoked = await create_invite(tenant.id, StaffRole.STAFF, "Ушёл", "LEFT", invited_by=manager_id)
    await revoke_invite(revoked.invite_id, tenant_id=tenant.id, actor_user_id=manager_id)
    assert await create_invite(tenant.id, StaffRole.STAFF, "Снова", "LEFT", invited_by=manager_id)


async def test_accept_always_creates_new_user(hotel: tuple[Tenant, uuid.UUID]) -> None:
    """Ревизия ADR-008: слияния «тот же человек во втором отеле» нет — без
    email его не по чему делать. Второе приглашение тому же человеку в другой
    отель — вторая учётка со своим логином."""
    tenant, manager_id = hotel
    first = await create_invite(
        tenant.id, StaffRole.MANAGER, "Боранбай", "BORM", invited_by=manager_id
    )
    second = await create_invite(
        await _other_hotel(), StaffRole.STAFF, "Боранбай", "BORM", invited_by=manager_id
    )

    here = await accept_invite(first.invite_token, password=PASSWORD)
    there = await accept_invite(second.invite_token, password=PASSWORD)

    assert here.user_id != there.user_id
    async with platform_session_scope() as session:
        memberships = (
            await session.scalars(
                select(TenantMembership).where(
                    TenantMembership.user_id.in_([here.user_id, there.user_id])
                )
            )
        ).all()
    assert {(m.user_id, m.role_key) for m in memberships} == {
        (here.user_id, StaffRole.MANAGER),
        (there.user_id, StaffRole.STAFF),
    }


async def test_login_taken_between_issue_and_accept_is_invalid_invite(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    """Spec 0037 §4: гонку двух менеджеров с одним логином выпуск не ловит —
    её ловит UNIQUE идентичности при принятии → ERR-AUTH-004, и ничего не
    создаётся (ни User, ни членство), а инвайт остаётся непринятым."""
    tenant, manager_id = hotel
    first = await create_invite(tenant.id, StaffRole.STAFF, "Первый", "TWIN", invited_by=manager_id)
    # Второе приглашение с тем же логином — так, как его оставила бы гонка
    # двух менеджеров (проверка выпуска прошла у обоих до записи).
    racing_token = secrets.token_urlsafe(32)
    async with platform_session_scope() as session:
        racing = StaffInvite(
            tenant_id=tenant.id,
            role_key=StaffRole.STAFF,
            invited_name="Второй",
            login="TWIN",
            token_hash=_hash_token(racing_token),
            invited_by=manager_id,
            expires_at=utc_now() + timedelta(hours=1),
        )
        session.add(racing)
        await session.flush()
        racing_id = racing.id
    await accept_invite(first.invite_token, password=PASSWORD)
    users_before = await _user_count()

    with pytest.raises(AppError) as error:
        await accept_invite(racing_token, password=PASSWORD)

    assert error.value.code == ERR_AUTH_INVITE_INVALID
    assert await _user_count() == users_before
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, racing_id)
        assert invite is not None and invite.accepted_at is None


async def test_invite_without_login_is_dead(hotel: tuple[Tenant, uuid.UUID]) -> None:
    """Spec 0037 §6: приглашение без логина (до перехода или от старого образа
    при откате) — мёртвое, как истёкшее: страница и принятие → ERR-AUTH-004,
    в списке ожидающих его нет. Код не полагается на отзыв в миграции."""
    tenant, manager_id = hotel
    token = secrets.token_urlsafe(32)
    async with platform_session_scope() as session:
        session.add(
            StaffInvite(
                tenant_id=tenant.id,
                role_key=StaffRole.STAFF,
                invited_name="Старый",
                token_hash=_hash_token(token),
                invited_by=manager_id,
                expires_at=utc_now() + timedelta(hours=1),
            )
        )

    assert await describe_invite(token) is None
    assert await list_pending_invites(tenant.id) == []
    with pytest.raises(AppError) as error:
        await accept_invite(token, password=PASSWORD)
    assert error.value.code == ERR_AUTH_INVITE_INVALID


async def test_short_password_rejected_and_invite_kept(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    grant = await create_invite(tenant.id, StaffRole.STAFF, "Ким", "KIMA", invited_by=manager_id)
    with pytest.raises(AppError) as error:
        await accept_invite(grant.invite_token, password="short")
    assert error.value.code == ERR_AUTH_PASSWORD_TOO_SHORT
    assert await describe_invite(grant.invite_token) is not None  # ссылка жива
