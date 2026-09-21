"""Ссылка привязки с талона заселения (spec 0033 §6/§10, issue #354).

Талон печатается и отдаётся гостю вместе с ключом, поэтому ссылка ведёт себя
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
    """Один талон — вся семья: каждое сканирование рождает свою сессию."""
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
    """Своего срока нет: продление Stay продлевает и талон, без перевыпуска."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        token = await issue_bind_link(result.stay.id)
        await extend_stay(result.stay.id, result.stay.check_out_at + timedelta(days=30))
        assert await start_guest_session_by_bind_link(_bind_data(token)) is not None


async def test_link_dies_when_stay_time_is_up(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Срок Stay вышел, а выезд не нажали — талон мёртв сам (ADR-008 §3)."""
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
    """«Гость потерял талон»: перевыпуск гасит и код, и все QR этого Stay;
    ссылка, выпущенная после перевыпуска, работает (новый талон)."""
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
    """«Показать QR» у стойки выпускает ещё одну ссылку — талон в номере жив."""
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
