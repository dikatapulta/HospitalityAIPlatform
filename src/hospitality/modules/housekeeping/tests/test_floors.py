"""Раздача этажей в модуле housekeeping (spec 0053 §3, §6, §10 «Модуль»).

Этаж номера на границах нумерации; «Сохранить раздачу» заменяет раздачу отеля
целиком, повтор — та же раздача; чужой отель её не видит и не стирает.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.housekeeping.tests.conftest import make_acting_user
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context


@pytest.mark.parametrize(
    ("room", "floor"),
    [
        ("704", 7),
        ("1204", 12),
        ("2601", 26),
        (" 305 ", 3),
        ("9999", 99),
        ("0704", 7),
        # Этажа нет — заявку слышит вся служба (§3).
        ("050", None),
        ("12", None),
        ("10001", None),
        ("Люкс", None),
        ("A-704", None),
        ("704a", None),
        ("７０４", None),  # полноширинные цифры: только ASCII
        ("", None),
        (None, None),
    ],
)
def test_floor_of_room_on_numbering_bounds(room: str | None, floor: int | None) -> None:
    assert housekeeping_api.floor_of_room(room) == floor


async def _users(count: int) -> list[uuid.UUID]:
    return [(await make_acting_user(f"Горничная {index}")).user_id for index in range(count)]


async def _distribution() -> dict[uuid.UUID, set[int]]:
    distribution: dict[uuid.UUID, set[int]] = {}
    for row in await housekeeping_api.list_floor_assignments():
        distribution.setdefault(row.user_id, set()).add(row.floor)
    return distribution


async def test_replace_swaps_whole_distribution(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Вторая раздача заменяет первую целиком: кого нет в новой — без этажей;
    пустой набор — без строк; повтор той же раздачи ничего не меняет."""
    tenant, _ = two_tenants
    aigul, dina, saule = await _users(3)
    with tenant_context(tenant):
        await housekeeping_api.replace_floor_assignments(
            {aigul: frozenset({7, 8}), dina: frozenset({9}), saule: frozenset({10})}
        )
        written = await housekeeping_api.replace_floor_assignments(
            {aigul: frozenset({9}), dina: frozenset({9, 11}), saule: frozenset()}
        )
        first = await _distribution()
        await housekeeping_api.replace_floor_assignments(
            {aigul: frozenset({9}), dina: frozenset({9, 11})}
        )
        again = await _distribution()

    assert written == 3
    assert first == {aigul: {9}, dina: {9, 11}}
    assert again == first


async def test_distribution_is_isolated_between_hotels(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Сохранение раздачи одного отеля не видит и не стирает раздачу другого (P-4)."""
    tenant_a, tenant_b = two_tenants
    aigul, dina = await _users(2)
    with tenant_context(tenant_b):
        await housekeeping_api.replace_floor_assignments({dina: frozenset({3})})
    with tenant_context(tenant_a):
        await housekeeping_api.replace_floor_assignments({aigul: frozenset({7})})
        await housekeeping_api.replace_floor_assignments({})
        seen_by_a = await _distribution()
    with tenant_context(tenant_b):
        seen_by_b = await _distribution()

    assert seen_by_a == {}
    assert seen_by_b == {dina: {3}}


async def test_floor_out_of_range_is_refused_by_database(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """CHECK 1–99 — страховка за проверкой кабинета."""
    tenant, _ = two_tenants
    (aigul,) = await _users(1)
    with tenant_context(tenant), pytest.raises(IntegrityError):
        async with session_scope() as session:
            await session.execute(
                text(
                    "INSERT INTO housekeeping_floor_assignments "
                    "(tenant_id, user_id, floor, assigned_at) VALUES (:tenant, :user, 100, now())"
                ),
                {"tenant": tenant, "user": aigul},
            )
