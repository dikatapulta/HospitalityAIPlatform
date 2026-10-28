"""CLI суперадмина (spec 0044 §8): заведение, потеря телефона, забытый пароль.

«Приложение на телефоне» в тестах — функция, которая сама считает код из
показанного секрета; неверный код — строка, которая заведомо не подходит.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hospitality.platform.events import SuperadminActionKind
from hospitality.platform.models import (
    StaffRole,
    StaffSecondFactor,
    Tenant,
    TenantMembership,
    User,
)
from hospitality.platform.staff_auth import resolve_staff_session
from hospitality.platform.staff_superadmin import superadmin_login
from hospitality.platform.totp import code_at, time_step
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.tools.superadmin import (
    SuperadminError,
    create_superadmin,
    main,
    reset_factor,
    set_password,
)
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login
from tests.test_staff_superadmin import outbox_kinds

SLUG = "hotel-cli"
shown_secrets: list[str] = []


def phone(secret: str, uri: str) -> str:
    assert uri.startswith("otpauth://totp/")
    shown_secrets.append(secret)
    return code_at(secret, time_step(utc_now()))


def wrong_phone(secret: str, uri: str) -> str:
    del uri
    return str((int(code_at(secret, time_step(utc_now()))) + 1) % 1_000_000).zfill(6)


@pytest.fixture
async def tenant(canonical_database: None) -> Tenant:
    async with platform_session_scope() as session:
        row = Tenant(slug=SLUG, name="Hotel CLI")
        session.add(row)
        await session.flush()
        return row


async def test_create_writes_superadmin_after_right_code(tenant: Tenant) -> None:
    staff_login = unique_login()
    lines = await create_superadmin(SLUG, staff_login, "Основатель", PASSWORD, phone)

    assert any(f"/staff/{SLUG}/superadmin" in line for line in lines)
    async with platform_session_scope() as session:
        user = await session.scalar(select(User).where(User.is_platform_admin.is_(True)))
        assert user is not None
        membership = await session.scalar(
            select(TenantMembership).where(TenantMembership.user_id == user.id)
        )
        factor = await session.get(StaffSecondFactor, user.id)
    assert membership is not None and membership.role_key is StaffRole.MANAGER
    assert factor is not None and factor.totp_secret == shown_secrets[-1]
    # Код подтверждения уже израсходован: тот же шаг на входе — повтор.
    assert factor.last_used_step == time_step(utc_now())
    assert await outbox_kinds() == [SuperadminActionKind.CREATED_ON_SERVER.value]


async def test_create_with_wrong_code_writes_nothing(tenant: Tenant) -> None:
    with pytest.raises(SuperadminError, match="ничего не записано"):
        await create_superadmin(SLUG, unique_login(), "Основатель", PASSWORD, wrong_phone)
    async with platform_session_scope() as session:
        assert await session.scalar(select(User)) is None


async def test_create_refuses_taken_login(tenant: Tenant) -> None:
    staff_login = unique_login()
    await create_staff_user(staff_login, tenant_id=tenant.id, role=StaffRole.STAFF)
    with pytest.raises(SuperadminError, match="уже занят"):
        await create_superadmin(SLUG, staff_login, "Основатель", PASSWORD, phone)


async def test_reset_factor_replaces_secret_and_kills_sessions(tenant: Tenant) -> None:
    staff_login = unique_login()
    await create_superadmin(SLUG, staff_login, "Основатель", PASSWORD, phone)
    old_secret = shown_secrets[-1]
    # Следующий шаг, чтобы код входа не совпал с израсходованным при заведении.
    async with platform_session_scope() as session:
        factor = await session.scalar(select(StaffSecondFactor))
        assert factor is not None
        factor.last_used_step = time_step(utc_now()) - 2
    grant = await superadmin_login(
        tenant.id,
        staff_login,
        PASSWORD,
        code_at(old_secret, time_step(utc_now())),
        client_ip="ip-cli",
    )

    await reset_factor(SLUG, staff_login, phone)

    assert shown_secrets[-1] != old_secret
    assert await resolve_staff_session(grant.session_token) is None
    with pytest.raises(AppError):
        await superadmin_login(
            tenant.id,
            staff_login,
            PASSWORD,
            code_at(old_secret, time_step(utc_now()) + 1),
            client_ip="ip-cli",
        )
    # Старый телефон после замены — «пароль верный, код нет»: основатель узнает.
    assert (await outbox_kinds())[-2:] == [
        SuperadminActionKind.FACTOR_RESET_ON_SERVER.value,
        SuperadminActionKind.LOGIN_BAD_CODE.value,
    ]


async def test_set_password_and_unknown_superadmin(tenant: Tenant) -> None:
    staff_login = unique_login()
    await create_superadmin(SLUG, staff_login, "Основатель", PASSWORD, phone)
    await set_password(SLUG, staff_login, "another-long-password")
    assert (await outbox_kinds())[-1] == SuperadminActionKind.PASSWORD_SET_ON_SERVER.value
    regular = unique_login()
    await create_staff_user(regular, tenant_id=tenant.id, role=StaffRole.MANAGER)
    with pytest.raises(SuperadminError, match="нет"):
        await set_password(SLUG, regular, "another-long-password")


def test_main_reports_password_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = iter(["first-password", "second-password"])
    monkeypatch.setattr(
        "hospitality.tools.superadmin.getpass.getpass", lambda _prompt: next(prompts)
    )
    assert main(["create", "ADMN", "--name", "Основатель", "--tenant-slug", SLUG]) == 1
    assert "Пароли не совпали" in capsys.readouterr().err
