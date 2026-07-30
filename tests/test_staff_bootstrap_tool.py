"""CLI бутстрапа первого менеджера (spec 0033 §3.3, §10): единственный путь
появления первого `manager` тенанта — дальше только приглашения из кабинета.

Работа с БД — через `bootstrap_manager` на живом демо-тенанте; обвязка `main`
(getpass, коды возврата) — с заглушками (канон test_checkin_tool).
"""

from __future__ import annotations

import pytest

from hospitality.platform.models import StaffRole
from hospitality.platform.seed import DEMO_TENANT_SLUG, seed_demo_tenant
from hospitality.platform.staff_auth import login
from hospitality.tools.staff_bootstrap import BootstrapError, bootstrap_manager, main
from tests.test_staff_auth import PASSWORD, _unique_email, _unique_ip


async def test_bootstrap_creates_manager_who_can_login(canonical_database: None) -> None:
    await seed_demo_tenant()
    email = _unique_email()

    lines = await bootstrap_manager(DEMO_TENANT_SLUG, f" {email.upper()} ", "Аружан", PASSWORD)
    assert any("Менеджер" in line for line in lines)

    grant = await login(email, PASSWORD, client_ip=_unique_ip())
    assert grant.display_name == "Аружан"
    assert [m.role_key for m in grant.memberships] == [StaffRole.MANAGER]
    assert grant.memberships[0].tenant_slug == DEMO_TENANT_SLUG


async def test_bootstrap_refuses_existing_email(canonical_database: None) -> None:
    await seed_demo_tenant()
    email = _unique_email()
    await bootstrap_manager(DEMO_TENANT_SLUG, email, "Аружан", PASSWORD)

    with pytest.raises(BootstrapError, match="уже зарегистрирован"):
        await bootstrap_manager(DEMO_TENANT_SLUG, email, "Аружан", PASSWORD)


async def test_bootstrap_unknown_tenant_fails(canonical_database: None) -> None:
    with pytest.raises(BootstrapError, match="не найден"):
        await bootstrap_manager("no-such-hotel", _unique_email(), "Аружан", PASSWORD)


def test_main_reports_password_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = iter(["first-password", "second-password"])
    monkeypatch.setattr(
        "hospitality.tools.staff_bootstrap.getpass.getpass", lambda _prompt: next(prompts)
    )

    exit_code = main(["manager@hotel.kz", "--name", "Аружан"])

    assert exit_code == 1
    assert "не совпали" in capsys.readouterr().err


def test_main_prints_short_password_error_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ожидаемый отказ ядра (ERR-AUTH-007) — текст с кодом каталога, не трассировка."""
    monkeypatch.setattr(
        "hospitality.tools.staff_bootstrap.getpass.getpass", lambda _prompt: "short"
    )

    exit_code = main(["manager@hotel.kz", "--name", "Аружан"])

    assert exit_code == 1
    assert "ERR-AUTH-007" in capsys.readouterr().err
