"""CLI бутстрапа первого менеджера (spec 0033 §3.3, §10; spec 0037 §6, §10):
единственный путь появления первого `manager` тенанта — дальше только
приглашения из кабинета. Менеджер заводится логином отеля.

Работа с БД — через `bootstrap_manager` на живом демо-тенанте; обвязка `main`
(getpass, коды возврата) — с заглушками (канон test_checkin_tool).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hospitality.platform.models import StaffRole, User
from hospitality.platform.seed import DEMO_TENANT_SLUG, seed_demo_tenant
from hospitality.platform.staff_auth import find_hotel, login
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_INVALID
from hospitality.platform.staff_invites import create_invite
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.tools.staff_bootstrap import BootstrapError, bootstrap_manager, main
from tests.test_staff_auth import PASSWORD, _unique_ip, unique_login


async def test_bootstrap_creates_manager_who_can_login(canonical_database: None) -> None:
    await seed_demo_tenant()
    staff_login = unique_login()

    lines = await bootstrap_manager(
        DEMO_TENANT_SLUG, f" {staff_login.lower()} ", "Аружан", PASSWORD
    )
    assert any("Менеджер" in line for line in lines)
    assert any(f"/staff/{DEMO_TENANT_SLUG}/login" in line for line in lines)

    hotel = await find_hotel(DEMO_TENANT_SLUG)
    assert hotel is not None
    grant = await login(hotel.tenant_id, staff_login, PASSWORD, client_ip=_unique_ip())
    assert grant.display_name == "Аружан"
    assert [m.role_key for m in grant.memberships] == [StaffRole.MANAGER]
    assert grant.memberships[0].tenant_slug == DEMO_TENANT_SLUG


async def test_bootstrap_refuses_taken_login(canonical_database: None) -> None:
    await seed_demo_tenant()
    staff_login = unique_login()
    await bootstrap_manager(DEMO_TENANT_SLUG, staff_login, "Аружан", PASSWORD)

    with pytest.raises(BootstrapError, match="уже занят"):
        await bootstrap_manager(DEMO_TENANT_SLUG, staff_login.lower(), "Аружан", PASSWORD)


async def test_bootstrap_refuses_login_of_pending_invite(canonical_database: None) -> None:
    """Та же проверка занятости, что у приглашения (spec 0037 §4): иначе
    ожидающая ссылка с этим логином после бутстрапа уже не принялась бы."""
    await seed_demo_tenant()
    hotel = await find_hotel(DEMO_TENANT_SLUG)
    assert hotel is not None
    manager_login = unique_login()
    await bootstrap_manager(DEMO_TENANT_SLUG, manager_login, "Аружан", PASSWORD)
    async with platform_session_scope() as session:
        manager_id = await session.scalar(select(User.id).where(User.display_name == "Аружан"))
    assert manager_id is not None
    await create_invite(hotel.tenant_id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager_id)

    with pytest.raises(BootstrapError, match="уже занят"):
        await bootstrap_manager(DEMO_TENANT_SLUG, "dana", "Дана", PASSWORD)


async def test_bootstrap_refuses_wrong_login_format(canonical_database: None) -> None:
    await seed_demo_tenant()
    with pytest.raises(AppError) as error:
        await bootstrap_manager(DEMO_TENANT_SLUG, "manager@hotel.kz", "Аружан", PASSWORD)
    assert error.value.code == ERR_AUTH_LOGIN_INVALID


async def test_bootstrap_unknown_tenant_fails(canonical_database: None) -> None:
    with pytest.raises(BootstrapError, match="не найден"):
        await bootstrap_manager("no-such-hotel", unique_login(), "Аружан", PASSWORD)


def test_main_reports_password_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = iter(["first-password", "second-password"])
    monkeypatch.setattr(
        "hospitality.tools.staff_bootstrap.getpass.getpass", lambda _prompt: next(prompts)
    )

    exit_code = main(["BORM", "--name", "Аружан"])

    assert exit_code == 1
    assert "не совпали" in capsys.readouterr().err


def test_main_prints_short_password_error_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ожидаемый отказ ядра (ERR-AUTH-007) — текст с кодом каталога, не трассировка."""
    monkeypatch.setattr(
        "hospitality.tools.staff_bootstrap.getpass.getpass", lambda _prompt: "short"
    )

    exit_code = main(["BORM", "--name", "Аружан"])

    assert exit_code == 1
    assert "ERR-AUTH-007" in capsys.readouterr().err
