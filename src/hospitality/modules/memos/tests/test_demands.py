"""Требование объяснения (spec 0077 §6, §12 PR 5): фото, «Получил(а)» только сотруднику,
срок по поясу отеля, права старшего — только свои службы, фото уходит с требованием."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from hospitality.modules.memos import api as memos_api
from hospitality.modules.memos.models import Memo, MemoDemand
from hospitality.modules.memos.service import hotel_config
from hospitality.modules.memos.tests.conftest import Hotel, act, configure
from hospitality.platform import files
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_confirm import ConfirmationMethod, ProofStatus
from hospitality.platform.staff_team import change_member_access
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.passkey_authenticator import SoftAuthenticator
from tests.test_file_content import make_png
from tests.test_staff_auth import PASSWORD

State = memos_api.DemandState
PHOTO = make_png((800, 1100))


async def _code(call: Awaitable[object]) -> str:
    with pytest.raises(AppError) as error:
        await call
    return error.value.code


async def demand(hotel: Hotel, by: StaffContext | None = None) -> uuid.UUID:
    """Требование менеджера (по умолчанию) у автора — у него есть ключ устройства."""
    with tenant_context(hotel.tenant_id):
        return await memos_api.create_demand(
            by or hotel.boss, hotel.author.user_id, PHOTO, "demand.jpg"
        )


async def receive(
    hotel: Hotel,
    staff: StaffContext,
    demand_id: uuid.UUID,
    *,
    device: SoftAuthenticator | None = None,
) -> datetime:
    """«Получил(а)» целиком: ключом устройства, если он дан, иначе паролем."""
    with tenant_context(hotel.tenant_id):
        request = await memos_api.begin_receipt(staff, demand_id)
        answer: memos_api.PasskeyAnswer | memos_api.PasswordAnswer
        if device is not None:
            answer = memos_api.PasskeyAnswer(
                challenge_id=request.challenge_id, credential_json=device.sign(request.options_json)
            )
        else:
            answer = memos_api.PasswordAnswer(
                challenge_id=request.challenge_id, password=PASSWORD, client_ip="ip-memos"
            )
        return await memos_api.confirm_receipt(staff, demand_id, answer)


async def _card(hotel: Hotel, staff: StaffContext, demand_id: uuid.UUID) -> memos_api.DemandCard:
    with tenant_context(hotel.tenant_id):
        return await memos_api.get_demand(staff, demand_id)


async def _box(hotel: Hotel, staff: StaffContext, box: memos_api.MemoBox) -> list[uuid.UUID]:
    with tenant_context(hotel.tenant_id):
        return [row.id for row in await memos_api.list_demands(staff, box)]


async def test_demand_is_received_only_by_its_employee(hotel: Hotel) -> None:
    """«Ждут меня» у сотрудника до «Получил(а)»; затребовавшему и постороннему кнопки нет;
    получение — с подтверждением ключом, срок — в карточке; повтор — ERR-MEMO-002."""
    demand_id = await demand(hotel)
    card = await _card(hotel, hotel.boss, demand_id)
    assert card.state is State.AWAITING_RECEIPT and card.received_at is None
    assert await _box(hotel, hotel.author, memos_api.MemoBox.WAITING) == [demand_id]
    assert await _box(hotel, hotel.boss, memos_api.MemoBox.WAITING) == []
    assert await _box(hotel, hotel.boss, memos_api.MemoBox.MINE) == [demand_id]
    assert await _box(hotel, hotel.author, memos_api.MemoBox.MINE) == []

    with tenant_context(hotel.tenant_id):
        assert await _code(memos_api.begin_receipt(hotel.boss, demand_id)) == "ERR-MEMO-001"
        assert await _code(memos_api.begin_receipt(hotel.outsider, demand_id)) == "ERR-MEMO-001"
        assert await _code(memos_api.get_demand(hotel.outsider, demand_id)) == "ERR-MEMO-001"
        assert await _code(memos_api.read_demand_photo(hotel.outsider, demand_id)) == (
            "ERR-MEMO-001"
        )
        # Без образца подписи «Получил(а)» не начинается.
        await memos_api.revoke_signature_sample(hotel.author.user_id)
        assert await _code(memos_api.begin_receipt(hotel.author, demand_id)) == "ERR-MEMO-003"
        await memos_api.set_signature_sample(
            hotel.author, memos_api.SignatureStrokes(strokes=(((1, 1), (9, 9)),))
        )

    due_at = await receive(hotel, hotel.author, demand_id, device=hotel.author_device)

    card = await _card(hotel, hotel.boss, demand_id)
    assert card.state is State.OPEN and card.due_at == due_at
    assert card.received_at is not None and card.received_at < card.due_at
    assert card.received_method is ConfirmationMethod.PASSKEY
    assert card.received_proof_status is ProofStatus.VERIFIED
    assert await _box(hotel, hotel.author, memos_api.MemoBox.WAITING) == []
    assert await _box(hotel, hotel.author, memos_api.MemoBox.ALL) == [demand_id]
    with tenant_context(hotel.tenant_id):
        assert await _code(memos_api.begin_receipt(hotel.author, demand_id)) == "ERR-MEMO-002"
        for staff in (hotel.boss, hotel.author):
            photo = await memos_api.read_demand_photo(staff, demand_id)
            assert photo.meta.purpose is files.FilePurpose.MEMO_DEMAND
            assert photo.meta.content_type == "image/webp"


async def test_receipt_proof_breaks_when_demand_is_changed_in_database(hotel: Hotel) -> None:
    """Доказательство получения привязано к требованию: подменённое фото — не «сходится»."""
    demand_id = await demand(hotel)
    await receive(hotel, hotel.author, demand_id, device=hotel.author_device)
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            await session.execute(
                update(MemoDemand).where(MemoDemand.id == demand_id).values(file_id=uuid.uuid4())
            )
    card = await _card(hotel, hotel.author, demand_id)
    assert card.received_proof_status is ProofStatus.MISMATCH


async def test_due_date_counts_working_days_in_hotel_zone(hotel: Hotel) -> None:
    """Срок — конец второго рабочего дня после дня получения по поясу отеля (§6.3)."""
    holiday = (utc_now() + timedelta(days=1)).date()
    await configure(hotel.tenant_id, holidays=[holiday.isoformat()])
    demand_id = await demand(hotel)
    due_at = await receive(hotel, hotel.author, demand_id)
    card = await _card(hotel, hotel.author, demand_id)
    zone = (await hotel_config(hotel.tenant_id)).tzinfo
    assert card.received_at is not None
    assert due_at == memos_api.explanation_due(card.received_at, zone, (holiday,))
    local_due = card.due_at.astimezone(zone) if card.due_at else None
    assert local_due is not None and (local_due.hour, local_due.minute) == (23, 59)


async def _set_services(hotel: Hotel, staff: StaffContext, role: StaffRole, *keys: str) -> None:
    await change_member_access(
        staff.user_id,
        hotel.tenant_id,
        role,
        service_keys=list(keys),
        actor_user_id=hotel.boss.user_id,
    )


async def _files(hotel: Hotel) -> int:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            return int(await session.scalar(select(func.count(files.UploadedFile.id))) or 0)


async def test_senior_demands_only_within_own_services(hotel: Hotel) -> None:
    """`senior` — только у `staff` и `senior` своих служб; `staff` не требует вовсе; отказ —
    до песочницы: файл на томе не остаётся."""
    await _set_services(hotel, hotel.senior, StaffRole.SENIOR, "housekeeping")
    await _set_services(hotel, hotel.signer_a, StaffRole.STAFF, "housekeeping")
    await _set_services(hotel, hotel.signer_b, StaffRole.STAFF, "reception")
    senior = hotel.senior.model_copy(update={"service_keys": frozenset({"housekeeping"})})

    with tenant_context(hotel.tenant_id):
        own = await memos_api.create_demand(senior, hotel.signer_a.user_id, PHOTO, "a.jpg")
        before = await _files(hotel)
        for target, reason in (
            (hotel.signer_b, "not_your_service"),
            (hotel.boss, "not_your_service"),
            (hotel.senior, "not_a_member"),
        ):
            with pytest.raises(AppError) as error:
                await memos_api.create_demand(senior, target.user_id, PHOTO, "b.jpg")
            assert error.value.code == "ERR-PLATFORM-002"
            assert (error.value.details or [{}])[0].get("reason") == reason
        staff_try = memos_api.create_demand(hotel.signer_a, hotel.author.user_id, PHOTO, "c.jpg")
        assert await _code(staff_try) == "ERR-AUTH-003"
        # Менеджер — у любого сотрудника отеля.
        await memos_api.create_demand(hotel.boss, hotel.signer_b.user_id, PHOTO, "d.jpg")
    assert await _files(hotel) == before + 1
    assert (await _card(hotel, hotel.signer_a, own)).demanded_by_id == hotel.senior.user_id


async def test_rejected_photo_leaves_no_demand(hotel: Hotel) -> None:
    """Не фото — ERR-FILES-002; требования без фото не бывает."""
    with tenant_context(hotel.tenant_id):
        bad = memos_api.create_demand(hotel.boss, hotel.author.user_id, b"%PDF-1.4", "x.pdf")
        assert await _code(bad) == "ERR-FILES-002"
    assert await _box(hotel, hotel.boss, memos_api.MemoBox.ALL) == []


async def _age_demand(hotel: Hotel, demand_id: uuid.UUID, *, days: int) -> None:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            await session.execute(
                update(MemoDemand)
                .where(MemoDemand.id == demand_id)
                .values(created_at=utc_now() - timedelta(days=days))
            )


async def test_retention_deletes_demand_with_photo_unless_explanation_is_kept(
    hotel: Hotel,
) -> None:
    """Требование — срок отеля от создания вместе с фото; пока хранится объяснительная по
    нему — живёт и требование."""
    await configure(hotel.tenant_id, memo_retention_days=365)
    lonely = await demand(hotel)
    answered = await demand(hotel)
    await receive(hotel, hotel.author, answered)
    with tenant_context(hotel.tenant_id):
        explanation = await memos_api.create_explanation(
            hotel.author,
            memos_api.ExplanationDraftInput(title="Опоздание", body="Пробка", demand_id=answered),
        )
    await act(hotel, hotel.author, explanation, memos_api.SendIntent())
    lonely_file = (await _row(hotel, lonely)).file_id
    for demand_id in (lonely, answered):
        await _age_demand(hotel, demand_id, days=366)

    with tenant_context(hotel.tenant_id):
        await memos_api.delete_expired_memos(drafts_before=utc_now() - timedelta(days=90))
        async with session_scope() as session:
            left = set(await session.scalars(select(MemoDemand.id)))
            linked = await session.scalar(select(Memo.demand_id).where(Memo.id == explanation))
        assert await _code(files.read_file(lonely_file)) == "ERR-FILES-004"
    assert left == {answered} and linked == answered


async def _row(hotel: Hotel, demand_id: uuid.UUID) -> MemoDemand:
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            row = await session.scalar(select(MemoDemand).where(MemoDemand.id == demand_id))
            assert row is not None
            return row
