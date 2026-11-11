"""Срок хранения данных о проживании и запись о согласии (spec 0032 §6, #948).

Возраст задаётся сдвигом `check_out_at` / `created_at` SQL'ом (канон
test_retention): ждать реальные дни нельзя, а подменять «сейчас» — значит
проверять не тот код, который поедет.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, text

from hospitality.modules.guests.api import (
    GuestSessionBind,
    GuestSessionStart,
    StayDataRetentionStats,
    check_out,
    delete_expired_stay_data,
    extend_stay,
    issue_bind_link,
    record_consent_evidence,
    resolve_identity_session,
    start_guest_session,
    start_guest_session_by_bind_link,
)
from hospitality.modules.guests.models import (
    ConsentRecord,
    Guest,
    GuestIdentity,
    GuestIdentityKind,
    GuestSession,
    Stay,
    StayAccessCode,
    StayBindLink,
)
from hospitality.modules.guests.tests.conftest import check_in_room
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

OLD = 91  # дней: за границей срока
FRESH = 1  # дней: внутри срока
CONSENT_DAYS = 1095

GUEST_TABLES = (Stay, StayAccessCode, StayBindLink, GuestSession, Guest, GuestIdentity)


def _days_ago(days: int) -> datetime:
    return utc_now() - timedelta(days=days)


async def _run(tenant_id: uuid.UUID) -> StayDataRetentionStats:
    with tenant_context(tenant_id):
        return await delete_expired_stay_data(
            checked_out_before=_days_ago(90), consents_before=_days_ago(CONSENT_DAYS)
        )


async def _counts(tenant_id: uuid.UUID) -> dict[str, int]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return {
                model.__tablename__: await session.scalar(select(func.count()).select_from(model))
                or 0
                for model in GUEST_TABLES
            }


async def _age_stay(tenant_id: uuid.UUID, stay_id: uuid.UUID, days: int) -> None:
    """Stay «выехал `days` назад», его гость «заведён» ещё на день раньше."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE stays SET check_out_at = :at WHERE id = :id"),
                {"at": _days_ago(days), "id": stay_id},
            )
            await session.execute(
                text(
                    "UPDATE guests SET created_at = :at "
                    "WHERE id = (SELECT guest_id FROM stays WHERE id = :id)"
                ),
                {"at": _days_ago(days + 1), "id": stay_id},
            )


def _by_code(
    code: str, *, kind: GuestIdentityKind = GuestIdentityKind.WEB, external_id: str | None = None
) -> GuestSessionStart:
    return GuestSessionStart(
        room_number="101",
        code=code,
        identity_kind=kind,
        identity_external_id=external_id or str(uuid.uuid4()),
        consent_version="2026-07-29-v3",
    )


async def _consent_records(tenant_id: uuid.UUID) -> list[ConsentRecord]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(select(ConsentRecord).order_by(ConsentRecord.consent_at))
            return list(rows)


async def test_expired_stay_goes_with_codes_links_sessions_and_guest(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Выезд старше срока: Stay, код, ссылка, сессия, гость и идентичность удалены;
    идентичность названа в итоге — по ней канал обнулит ссылку диалога."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        grant = await start_guest_session(_by_code(result.access_code))
        assert grant is not None
        await issue_bind_link(result.stay.id)
        await check_out(result.stay.id)
    await _age_stay(tenant_a, result.stay.id, OLD)

    stats = await _run(tenant_a)

    assert (stats.stays_deleted, stats.guests_deleted) == (1, 1)
    assert stats.deleted_identity_ids == (grant.guest_identity_id,)
    assert set((await _counts(tenant_a)).values()) == {0}


async def test_fresh_and_extended_stays_survive(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Недавний выезд и продлённое проживание не трогаются: срок — от
    действующего `check_out_at`."""
    tenant_a, _ = two_tenants
    recent = await check_in_room(tenant_a, "101")
    extended = await check_in_room(tenant_a, "102")
    with tenant_context(tenant_a):
        await check_out(recent.stay.id)
    await _age_stay(tenant_a, recent.stay.id, FRESH)
    await _age_stay(tenant_a, extended.stay.id, OLD)
    with tenant_context(tenant_a):
        await extend_stay(extended.stay.id, utc_now() + timedelta(days=2))

    stats = await _run(tenant_a)

    assert (stats.stays_deleted, stats.guests_deleted) == (0, 0)
    counts = await _counts(tenant_a)
    assert (counts["stays"], counts["guests"]) == (2, 2)


async def test_forgotten_checked_in_stay_past_term_is_deleted(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Критерий от статуса не зависит: Stay, забытый в `checked_in` дольше срока,
    не живёт вечно — и комната освобождается."""
    tenant_a, _ = two_tenants
    zombie = await check_in_room(tenant_a)
    await _age_stay(tenant_a, zombie.stay.id, OLD)

    stats = await _run(tenant_a)

    assert stats.stays_deleted == 1
    assert (await check_in_room(tenant_a)).stay.room_number == "101"


async def test_guest_is_kept_until_created_before_the_term(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Гость без Stay, но заведённый внутри срока, остаётся до своего срока."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        async with session_scope() as session:
            await session.execute(
                text("UPDATE stays SET check_out_at = :at"), {"at": _days_ago(OLD)}
            )

    stats = await _run(tenant_a)

    assert (stats.stays_deleted, stats.guests_deleted) == (1, 0)
    assert (await _counts(tenant_a))["guests"] == 1
    assert result.stay.id is not None


async def test_returning_guest_keeps_live_binding_when_old_guest_is_deleted(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Чат Telegram вернувшегося гостя: идентичность заведена при прошлом
    проживании и висит на прошлом госте. Удаление прошлого гостя не разлогинивает
    живого — идентичность переезжает к гостю текущего Stay."""
    tenant_a, _ = two_tenants
    chat_id = "700100200"
    telegram = GuestIdentityKind.TELEGRAM
    past = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        assert await start_guest_session(
            _by_code(past.access_code, kind=telegram, external_id=chat_id)
        )
        await check_out(past.stay.id)
    await _age_stay(tenant_a, past.stay.id, OLD)
    current = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        assert await start_guest_session(
            _by_code(current.access_code, kind=telegram, external_id=chat_id)
        )

    stats = await _run(tenant_a)

    assert (stats.stays_deleted, stats.guests_deleted) == (1, 1)
    assert stats.deleted_identity_ids == ()
    with tenant_context(tenant_a):
        binding = await resolve_identity_session(telegram, chat_id)
        assert binding is not None
        assert binding.stay_id == current.stay.id
        async with session_scope() as session:
            identity_guest = await session.scalar(select(GuestIdentity.guest_id))
            current_guest = await session.scalar(
                select(Stay.guest_id).where(Stay.id == current.stay.id)
            )
    assert identity_guest == current_guest


async def test_binding_writes_consent_record_that_outlives_the_stay(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Привязка кодом и ссылкой пишет запись о согласии: канал, версия, комната и
    даты Stay — без имени. Stay и гость удалены — запись остаётся."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        assert await start_guest_session(_by_code(result.access_code))
        bind_token = await issue_bind_link(result.stay.id)
        assert await start_guest_session_by_bind_link(
            GuestSessionBind(
                bind_token=bind_token,
                identity_external_id=str(uuid.uuid4()),
                consent_version="2026-07-29-v3",
            )
        )
        assert await start_guest_session(
            _by_code(result.access_code, kind=GuestIdentityKind.TELEGRAM, external_id="42")
        )
        await check_out(result.stay.id)
    await _age_stay(tenant_a, result.stay.id, OLD)

    stats = await _run(tenant_a)

    assert (stats.stays_deleted, stats.consent_records_deleted) == (1, 0)
    records = await _consent_records(tenant_a)
    assert [record.channel for record in records] == ["web", "web", "telegram"]
    for record in records:
        assert record.consent_version == "2026-07-29-v3"
        assert record.room_number == "101"
        assert record.check_in_at == result.stay.check_in_at
        assert record.check_out_at == result.stay.check_out_at
    assert "display_name" not in ConsentRecord.__table__.columns
    assert "external_id" not in ConsentRecord.__table__.columns


async def test_consent_record_matches_the_session_moment(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Запись и сессия несут один момент согласия — это одно событие."""
    tenant_a, _ = two_tenants
    result = await check_in_room(tenant_a)
    with tenant_context(tenant_a):
        assert await start_guest_session(_by_code(result.access_code))
        async with session_scope() as session:
            session_moment = await session.scalar(select(GuestSession.consent_at))

    assert [record.consent_at for record in await _consent_records(tenant_a)] == [session_moment]


async def test_consent_without_stay_and_its_own_term(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Согласие Telegram до ввода кода: комнаты и дат нет. Свой срок — три года
    с даты согласия: старше — удалена, моложе — жива."""
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        for age_days in (CONSENT_DAYS + 1, CONSENT_DAYS - 1):
            await record_consent_evidence(
                channel="telegram", consent_version="2026-07-29-v3", consent_at=_days_ago(age_days)
            )

    stats = await _run(tenant_a)

    assert stats.consent_records_deleted == 1
    (left,) = await _consent_records(tenant_a)
    assert (left.channel, left.room_number, left.check_in_at, left.check_out_at) == (
        "telegram",
        None,
        None,
        None,
    )
    assert left.consent_at > _days_ago(CONSENT_DAYS)


async def test_other_tenant_is_untouched_and_second_run_finds_nothing(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """P-4: проход по отелю A не трогает просроченное у отеля B. P-8: повторный
    проход по A — ноль строк."""
    tenant_a, tenant_b = two_tenants
    for tenant_id in (tenant_a, tenant_b):
        result = await check_in_room(tenant_id)
        with tenant_context(tenant_id):
            assert await start_guest_session(_by_code(result.access_code))
            await issue_bind_link(result.stay.id)
        await _age_stay(tenant_id, result.stay.id, OLD)

    first = await _run(tenant_a)
    second = await _run(tenant_a)

    assert (first.stays_deleted, first.guests_deleted) == (1, 1)
    assert (second.stays_deleted, second.guests_deleted, second.consent_records_deleted) == (
        0,
        0,
        0,
    )
    assert second.deleted_identity_ids == ()
    assert set((await _counts(tenant_b)).values()) == {1}
    assert len(await _consent_records(tenant_b)) == 1
