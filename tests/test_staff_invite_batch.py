"""Приглашения списком (spec 0072 §4, §9 PR 2): весь список или ни одного, номер и
причина у плохой строки, одна транзакция, принятие — обычной дверью приглашения,
в логе — только id и число."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_auth import login
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_INVALID
from hospitality.platform.staff_invite_batch import (
    MAX_BATCH_INVITES,
    InviteRow,
    create_invites,
)
from hospitality.platform.staff_invites import accept_invite, create_invite, list_pending_invites
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging
from tests.test_logging import find_record
from tests.test_staff_auth import PASSWORD, _unique_ip, create_staff_user, unique_login


@pytest.fixture
async def hotel(canonical_database: None) -> tuple[Tenant, uuid.UUID]:
    """Тенант + менеджер, который приглашает (как в test_staff_invites.py)."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-a", name="Hotel A")
        session.add(tenant)
        await session.flush()
    manager_id = await create_staff_user(
        unique_login(), tenant_id=tenant.id, role=StaffRole.MANAGER
    )
    return tenant, manager_id


def _rows(*pairs: tuple[str, str]) -> list[InviteRow]:
    return [InviteRow(invited_name=name, login=staff_login) for name, staff_login in pairs]


async def _rejected_details(
    tenant_id: uuid.UUID, manager_id: uuid.UUID, rows: list[InviteRow]
) -> list[dict[str, Any]]:
    with pytest.raises(AppError) as error:
        await create_invites(
            tenant_id, StaffRole.STAFF, rows, invited_by=manager_id, service_keys=["reception"]
        )
    assert error.value.code == ERR_AUTH_LOGIN_INVALID
    assert error.value.status_code == 422
    assert error.value.details is not None
    return error.value.details


async def test_batch_issues_every_row_and_each_link_is_a_normal_invite(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    tenant, manager_id = hotel

    grants = await create_invites(
        tenant.id,
        StaffRole.STAFF,
        _rows(("Айгуль Сейтова", " aysa "), ("Иван Петров", "IVAP"), ("Ерлан", "ERLA")),
        invited_by=manager_id,
        service_keys=["housekeeping"],
    )

    assert [grant.login for grant in grants] == ["AYSA", "IVAP", "ERLA"]  # порядок строк
    assert len({grant.invite_token for grant in grants}) == 3
    pending = {item.login: item for item in await list_pending_invites(tenant.id)}
    assert set(pending) == {"AYSA", "IVAP", "ERLA"}
    assert pending["IVAP"].invited_name == "Иван Петров"
    assert pending["IVAP"].role_key is StaffRole.STAFF
    assert pending["IVAP"].service_keys == ["housekeeping"]

    # Новой двери нет: ссылка из списка принимается обычным `accept_invite`.
    accepted = await accept_invite(grants[1].invite_token, password=PASSWORD)
    assert accepted.login == "IVAP"
    assert accepted.role_key is StaffRole.STAFF
    session = await login(tenant.id, "ivap", PASSWORD, client_ip=_unique_ip())
    assert session.user_id == accepted.user_id
    assert {item.login for item in await list_pending_invites(tenant.id)} == {"AYSA", "ERLA"}


async def test_one_bad_row_issues_nothing_and_names_every_bad_row(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    """Формат, занят учёткой, занят ожидающим приглашением, повтор в списке —
    каждая строка со своим номером и причиной; ни одного приглашения."""
    tenant, manager_id = hotel
    account = unique_login()
    await create_staff_user(account, tenant_id=tenant.id, role=StaffRole.STAFF)
    await create_invite(tenant.id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager_id)
    pending_before = len(await list_pending_invites(tenant.id))

    details = await _rejected_details(
        tenant.id,
        manager_id,
        _rows(
            ("Хороший", "GOOD"),
            ("Кириллица", "ИЩКЬ"),
            ("Учётка", account.lower()),
            ("Ожидает", "dana"),
            ("Первый", "TWIN"),
            ("Второй", "twin"),
            ("Пусто", ""),
        ),
    )

    assert details == [
        {"row": 2, "reason": "format"},
        {"row": 3, "reason": "taken"},
        {"row": 4, "reason": "taken"},
        {"row": 6, "reason": "duplicate"},
        {"row": 7, "reason": "format"},
    ]
    assert len(await list_pending_invites(tenant.id)) == pending_before


async def test_repeat_of_a_taken_login_is_marked_as_repeat(
    hotel: tuple[Tenant, uuid.UUID],
) -> None:
    """Первая строка — «занят», следующая с тем же логином — «уже есть выше»:
    у менеджера одна правка на первой строке, а не две загадки."""
    tenant, manager_id = hotel
    await create_invite(tenant.id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager_id)

    details = await _rejected_details(
        tenant.id, manager_id, _rows(("Дана Первая", "DANA"), ("Дана Вторая", "DANA"))
    )

    assert details == [{"row": 1, "reason": "taken"}, {"row": 2, "reason": "duplicate"}]


async def test_login_of_another_hotel_is_free(hotel: tuple[Tenant, uuid.UUID]) -> None:
    tenant, manager_id = hotel
    async with platform_session_scope() as session:
        other = Tenant(slug="hotel-b", name="Hotel B")
        session.add(other)
        await session.flush()
    await create_invite(other.id, StaffRole.STAFF, "Чужая", "SAME", invited_by=manager_id)

    grants = await create_invites(
        tenant.id, StaffRole.MANAGER, _rows(("Своя", "SAME")), invited_by=manager_id
    )

    assert [grant.login for grant in grants] == ["SAME"]


@pytest.mark.parametrize("size", [0, MAX_BATCH_INVITES + 1])
async def test_batch_size_is_bounded(hotel: tuple[Tenant, uuid.UUID], size: int) -> None:
    tenant, manager_id = hotel
    rows = _rows(*((f"Имя {index}", f"L{index:04d}") for index in range(size)))

    with pytest.raises(ValueError, match="batch must have"):
        await create_invites(tenant.id, StaffRole.MANAGER, rows, invited_by=manager_id)


async def test_batch_log_has_ids_and_count_but_no_names_logins_or_tokens(
    hotel: tuple[Tenant, uuid.UUID], capsys: pytest.CaptureFixture[str]
) -> None:
    tenant, manager_id = hotel
    configure_logging()

    grants = await create_invites(
        tenant.id,
        StaffRole.STAFF,
        _rows(("Zarina Markerova", "ZMARK"), ("Oleg Markerov", "OMARK")),
        invited_by=manager_id,
        service_keys=["reception"],
    )

    # Вывод нужен целиком — для проверки утечки, поэтому записи разбираются здесь,
    # а не `read_log_records` (он читает capsys сам).
    output = capsys.readouterr().out
    records = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    created = find_record(records, "staff.invite_batch_created")
    assert created["invite_count"] == 2
    assert created["invite_ids"] == [str(grant.invite_id) for grant in grants]
    assert created["tenant_id"] == str(tenant.id)
    for secret in ("Markerov", "ZMARK", "OMARK", *(grant.invite_token for grant in grants)):
        assert secret not in output
