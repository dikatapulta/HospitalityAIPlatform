"""Ссылка привязки с листка гостю (spec 0033 §6/§10, issue #354).

Листок печатается и отдаётся гостю вместе с ключом, поэтому ссылка ведёт себя
как код заселения: многоразова, срок производен от Stay, гаснет перевыпуском
кода и выездом. Хранилище — Postgres (RLS), в БД только SHA-256 токена.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from hospitality.modules.guests.api import (
    ERR_GUESTS_STAY_NOT_FOUND,
    GuestIdentityKind,
    GuestSessionBind,
    check_out,
    extend_stay,
    issue_bind_link,
    move_stay,
    reissue_access_code,
    resolve_session,
    start_guest_session_by_bind_link,
)
from hospitality.modules.guests.models import GuestIdentity, GuestSession, Stay, StayBindLink
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


def _bind_data(token: str) -> GuestSessionBind:
    return GuestSessionBind(
        bind_token=token,
        identity_external_id=str(uuid.uuid4()),
        consent_version="v1",
    )


async def test_link_is_reusable_within_the_stay(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Один листок — вся семья: каждое сканирование рождает свою сессию."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
        first = await start_guest_session_by_bind_link(_bind_data(token))
        second = await start_guest_session_by_bind_link(_bind_data(token))
    assert first is not None and second is not None
    assert first.stay_id == second.stay_id == result.stay.id
    assert first.session_token != second.session_token


async def test_bind_creates_session_via_the_same_path(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Привязка по ссылке рождает ту же пару идентичность+сессия, что ввод
    кода (P-12): kind=web, согласие на сессии, resolve_session работает."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
        grant = await start_guest_session_by_bind_link(_bind_data(token))
        assert grant is not None
        assert grant.room_number == "101"

        active = await resolve_session(grant.session_token)
        assert active is not None
        assert active.stay_id == result.stay.id

        async with session_scope() as session:
            (identity,) = (await session.scalars(select(GuestIdentity))).all()
            (guest_session,) = (await session.scalars(select(GuestSession))).all()
            (link,) = (await session.scalars(select(StayBindLink))).all()
    assert identity.kind is GuestIdentityKind.WEB
    assert guest_session.consent_version == "v1"
    # Секрет в БД — только хэш (ADR-008): plaintext живёт лишь в QR.
    assert token not in link.token_hash
    assert len(link.token_hash) == 64


async def test_link_follows_stay_extension(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Своего срока нет: продление Stay продлевает и листок, без перевыпуска."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
        await extend_stay(result.stay.id, result.stay.check_out_at + timedelta(days=30))
        assert await start_guest_session_by_bind_link(_bind_data(token)) is not None


async def test_link_survives_room_move(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Ссылка привязана к Stay, а не к комнате: листок после переселения
    пускает гостя уже в новую комнату (spec 0033 §6, #360)."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
        await move_stay(result.stay.id, "202")
        grant = await start_guest_session_by_bind_link(_bind_data(token))
    assert grant is not None
    assert grant.room_number == "202"


async def test_link_dies_when_stay_time_is_up(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Срок Stay вышел, а выезд не нажали — листок мёртв сам (ADR-008 §3)."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
        async with session_scope() as session:
            await session.execute(
                update(Stay)
                .where(Stay.id == result.stay.id)
                .values(check_out_at=utc_now() - timedelta(minutes=1))
            )
        assert await start_guest_session_by_bind_link(_bind_data(token)) is None


async def test_checkout_kills_link(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
        await check_out(result.stay.id)
        assert await start_guest_session_by_bind_link(_bind_data(token)) is None
        async with session_scope() as session:
            (link,) = (await session.scalars(select(StayBindLink))).all()
    assert link.revoked_at is not None


async def test_code_reissue_kills_every_link_of_the_stay(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Гость потерял листок»: перевыпуск гасит и код, и все QR этого Stay;
    ссылка, выпущенная после перевыпуска, работает (новый листок)."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        printed = await issue_bind_link(result.stay.id)
        shown = await issue_bind_link(result.stay.id)
        await reissue_access_code(result.stay.id)
        assert await start_guest_session_by_bind_link(_bind_data(printed)) is None
        assert await start_guest_session_by_bind_link(_bind_data(shown)) is None

        fresh = await issue_bind_link(result.stay.id)
        assert await start_guest_session_by_bind_link(_bind_data(fresh)) is not None


async def test_new_link_does_not_kill_printed_one(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Показать QR» у стойки выпускает ещё одну ссылку — листок в номере жив."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        printed = await issue_bind_link(result.stay.id)
        await issue_bind_link(result.stay.id)
        assert await start_guest_session_by_bind_link(_bind_data(printed)) is not None


async def test_foreign_tenant_link_is_useless(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """RLS: токен отеля A в контексте отеля B не находит ничего (P-4)."""
    tenant_a, tenant_b = two_tenants
    result = await check_in_room(tenant_a)
    await check_in_room(tenant_b)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
    with tenant_context(tenant_b):
        assert await start_guest_session_by_bind_link(_bind_data(token)) is None
    with tenant_context(tenant_a):
        assert await start_guest_session_by_bind_link(_bind_data(token)) is not None


async def test_unknown_token_is_rejected(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        assert await start_guest_session_by_bind_link(_bind_data("not-a-token")) is None


async def test_issue_requires_active_stay(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        with pytest.raises(AppError) as error:
            await issue_bind_link(uuid.uuid4())
        assert error.value.code == ERR_GUESTS_STAY_NOT_FOUND


def _rescan_data(token: str, current_session_token: str) -> GuestSessionBind:
    return _bind_data(token).model_copy(update={"current_session_token": current_session_token})


async def test_rescan_from_same_browser_keeps_the_identity(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """#223: браузер с живой сессией того же Stay — новая сессия на прежнюю
    идентичность (диалог гостя прежний); без cookie — своя, как у второго телефона."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
        first = await start_guest_session_by_bind_link(_bind_data(token))
        assert first is not None
        rescan = await start_guest_session_by_bind_link(_rescan_data(token, first.session_token))
        other_phone = await start_guest_session_by_bind_link(_bind_data(token))
    assert rescan is not None and other_phone is not None
    assert rescan.guest_identity_id == first.guest_identity_id
    assert rescan.session_token != first.session_token
    assert other_phone.guest_identity_id != first.guest_identity_id


async def test_rescan_ignores_session_of_other_stay_revoked_or_garbage(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Сессия другого Stay, отозванная или мусор вместо токена — привязка как без
    cookie: чужой диалог к Stay ссылки не приезжает, битая cookie вход не роняет."""
    tenant_a, _ = two_tenants
    stay_101 = await check_in_room(tenant_a, "101")
    stay_102 = await check_in_room(tenant_a, "102")
    with tenant_context(tenant_a):
        token_101 = await issue_bind_link(stay_101.stay.id)
        token_102 = await issue_bind_link(stay_102.stay.id)
        in_101 = await start_guest_session_by_bind_link(_bind_data(token_101))
        assert in_101 is not None
        other_stay = await start_guest_session_by_bind_link(
            _rescan_data(token_102, in_101.session_token)
        )
        garbage = await start_guest_session_by_bind_link(_rescan_data(token_101, "x" * 5000))
        async with session_scope() as session:
            await session.execute(update(GuestSession).values(revoked_at=utc_now()))
        revoked = await start_guest_session_by_bind_link(
            _rescan_data(token_101, in_101.session_token)
        )
    assert other_stay is not None and revoked is not None and garbage is not None
    assert other_stay.stay_id == stay_102.stay.id
    assert other_stay.guest_identity_id != in_101.guest_identity_id
    assert garbage.guest_identity_id != in_101.guest_identity_id
    assert revoked.guest_identity_id != in_101.guest_identity_id
