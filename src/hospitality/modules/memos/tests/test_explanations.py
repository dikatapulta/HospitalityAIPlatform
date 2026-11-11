"""Объяснительная (spec 0077 §6, §12 PR 5): по требованию — после «Получил(а)», адресат —
затребовавший; «Подать» — номер «ОБ-…» и сразу у адресата, без решения; «Дополнить»;
после срока — с фактическим временем. Срок: выходные, праздник, пояс отеля."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import update

from hospitality.modules.memos import api as memos_api
from hospitality.modules.memos.models import MemoDemand
from hospitality.modules.memos.tests.conftest import Hotel, act
from hospitality.modules.memos.tests.test_demands import demand, receive
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_confirm import ProofStatus
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

Status = memos_api.MemoStatus
State = memos_api.DemandState
ALMATY = ZoneInfo("Asia/Almaty")  # UTC+5
SEND = memos_api.SendIntent()


@pytest.mark.parametrize(
    ("received_local", "holidays", "due_day"),
    [
        pytest.param(
            datetime(2026, 11, 11, 10, 0, tzinfo=ALMATY), (), date(2026, 11, 13), id="wednesday"
        ),
        pytest.param(
            datetime(2026, 11, 12, 18, 0, tzinfo=ALMATY), (), date(2026, 11, 16), id="over-weekend"
        ),
        pytest.param(
            datetime(2026, 11, 14, 12, 0, tzinfo=ALMATY), (), date(2026, 11, 17), id="on-saturday"
        ),
        pytest.param(
            datetime(2026, 12, 15, 9, 0, tzinfo=ALMATY),
            (date(2026, 12, 16), date(2026, 12, 17)),
            date(2026, 12, 21),
            id="independence-day-holidays",
        ),
    ],
)
def test_due_is_end_of_second_working_day(
    received_local: datetime, holidays: tuple[date, ...], due_day: date
) -> None:
    received = received_local.astimezone(UTC)
    due = memos_api.explanation_due(received, ALMATY, holidays)
    local = due.astimezone(ALMATY)
    assert local.date() == due_day and (local.hour, local.minute, local.second) == (23, 59, 59)
    assert due.tzinfo is UTC


def test_day_of_receipt_is_taken_in_hotel_zone() -> None:
    """Вторник 21:30 UTC — уже среда в Алматы: день получения — среда, срок — пятница."""
    received = datetime(2026, 11, 10, 21, 30, tzinfo=UTC)
    assert memos_api.explanation_due(received, ALMATY, ()).astimezone(ALMATY).date() == date(
        2026, 11, 13
    )
    assert memos_api.explanation_due(received, UTC, ()).date() == date(2026, 11, 12)


async def _code(call: Awaitable[object]) -> str:
    with pytest.raises(AppError) as error:
        await call
    return error.value.code


def _text(**fields: object) -> memos_api.ExplanationDraftInput:
    return memos_api.ExplanationDraftInput.model_validate(
        {"title": "Опоздание 11.11", "body": "Опоздал на 20 минут: авария на Аль-Фараби.", **fields}
    )


async def _card(hotel: Hotel, staff: StaffContext, memo_id: uuid.UUID) -> memos_api.MemoCard:
    with tenant_context(hotel.tenant_id):
        return await memos_api.get_memo(staff, memo_id)


async def _demand_card(hotel: Hotel, demand_id: uuid.UUID) -> memos_api.DemandCard:
    with tenant_context(hotel.tenant_id):
        return await memos_api.get_demand(hotel.boss, demand_id)


async def test_explanation_by_demand_goes_to_demander_without_decision(hotel: Hotel) -> None:
    """До «Получил(а)» писать нельзя; поданная — «ОБ-…», у затребовавшего без решения и не
    в «Ждут меня»; черновик он не видит; «Дополнить» — вторая со ссылкой на первую."""
    demand_id = await demand(hotel)
    with tenant_context(hotel.tenant_id):
        early = memos_api.create_explanation(hotel.author, _text(demand_id=demand_id))
        assert await _code(early) == "ERR-MEMO-002"
        foreign = memos_api.create_explanation(hotel.signer_a, _text(demand_id=demand_id))
        assert await _code(foreign) == "ERR-MEMO-001"
    await receive(hotel, hotel.author, demand_id, device=hotel.author_device)
    with tenant_context(hotel.tenant_id):
        memo_id = await memos_api.create_explanation(hotel.author, _text(demand_id=demand_id))
        await memos_api.update_explanation(
            hotel.author, memo_id, memos_api.ExplanationText(title="Опоздание", body="Авария.")
        )
        moved = memos_api.ExplanationText(title="x", body="y", addressee_id=hotel.senior.user_id)
        with pytest.raises(AppError) as error:
            await memos_api.update_explanation(hotel.author, memo_id, moved)
        assert (error.value.details or [{}])[0].get("reason") == "addressee_fixed"
        update_memo = memos_api.update_draft(hotel.author, memo_id, _memo_draft(hotel))
        assert await _code(update_memo) == "ERR-MEMO-001"
    assert [row.id for row in (await _demand_card(hotel, demand_id)).explanations] == []

    assert await act(hotel, hotel.author, memo_id, SEND, device=hotel.author_device) is (
        Status.SUBMITTED
    )

    card = await _card(hotel, hotel.boss, memo_id)
    assert card.kind is memos_api.MemoKind.EXPLANATION and card.addressee_id == hotel.boss.user_id
    assert card.number is not None and card.number.startswith("ОБ-")
    assert card.demand_id == demand_id and card.body == "Авария." and card.content_intact
    author_row = next(row for row in card.signatures if row.role is memos_api.SignatureRole.AUTHOR)
    assert author_row.proof_status is ProofStatus.VERIFIED
    decide = memos_api.DecisionIntent(decision=memos_api.MemoDecision.APPROVED)
    with tenant_context(hotel.tenant_id):
        assert await _code(memos_api.begin_action(hotel.boss, memo_id, decide)) == "ERR-MEMO-001"
        waiting = await memos_api.list_memos(hotel.boss, memos_api.MemoBox.WAITING)
        assert memo_id not in [row.id for row in waiting]
        everything = await memos_api.list_memos(hotel.boss, memos_api.MemoBox.ALL)
        assert memo_id in [row.id for row in everything]
        assert await _code(memos_api.get_memo(hotel.signer_a, memo_id)) == "ERR-MEMO-001"
    demand_card = await _demand_card(hotel, demand_id)
    assert demand_card.state is State.EXPLAINED
    assert [(row.id, row.late) for row in demand_card.explanations] == [(memo_id, False)]

    with tenant_context(hotel.tenant_id):
        extra_text = memos_api.ExplanationText(title="Дополнение", body="Справка ГАИ будет завтра.")
        supplement = await memos_api.create_supplement(hotel.author, memo_id, extra_text)
        twice = memos_api.create_supplement(hotel.author, supplement, extra_text)
        assert await _code(twice) == "ERR-MEMO-002"
    await act(hotel, hotel.author, supplement, SEND)
    extra = await _card(hotel, hotel.boss, supplement)
    assert extra.based_on_id == memo_id and extra.based_on_number == card.number
    assert extra.demand_id == demand_id and extra.number != card.number


async def test_explanation_after_due_is_late_and_expired_demand_says_so(hotel: Hotel) -> None:
    """Срок прошёл без объяснительной — «истёк»; поданная потом — с отметкой «после срока»."""
    demand_id = await demand(hotel)
    await receive(hotel, hotel.author, demand_id)
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            await session.execute(
                update(MemoDemand)
                .where(MemoDemand.id == demand_id)
                .values(due_at=utc_now() - timedelta(minutes=1))
            )
    assert (await _demand_card(hotel, demand_id)).state is State.EXPIRED
    with tenant_context(hotel.tenant_id):
        memo_id = await memos_api.create_explanation(hotel.author, _text(demand_id=demand_id))
    await act(hotel, hotel.author, memo_id, SEND)
    card = await _demand_card(hotel, demand_id)
    assert card.state is State.EXPLAINED
    assert [(row.id, row.late) for row in card.explanations] == [(memo_id, True)]


async def test_explanation_without_demand_goes_to_chosen_senior_or_manager(hotel: Hotel) -> None:
    """Без требования адресат — `senior` или `manager` по выбору автора; его можно сменить
    в черновике; `staff` адресатом быть не может."""
    with tenant_context(hotel.tenant_id):
        bad = memos_api.create_explanation(hotel.author, _text(addressee_id=hotel.signer_a.user_id))
        assert await _code(bad) == "ERR-PLATFORM-002"
        memo_id = await memos_api.create_explanation(
            hotel.author, _text(addressee_id=hotel.boss.user_id)
        )
        await memos_api.update_explanation(
            hotel.author,
            memo_id,
            memos_api.ExplanationText(
                title="Сам", body="Разбил вазу.", addressee_id=hotel.senior.user_id
            ),
        )
    await act(hotel, hotel.author, memo_id, SEND)
    card = await _card(hotel, hotel.senior, memo_id)
    assert card.addressee_id == hotel.senior.user_id and card.demand_id is None
    assert card.status is Status.SUBMITTED and card.number is not None
    with tenant_context(hotel.tenant_id):
        assert await _code(memos_api.get_memo(hotel.boss, memo_id)) == "ERR-MEMO-001"


def test_explanation_needs_exactly_one_basis() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        _text()
    with pytest.raises(ValueError, match="exactly one"):
        _text(demand_id=uuid.uuid4(), addressee_id=uuid.uuid4())


def _memo_draft(hotel: Hotel) -> memos_api.MemoDraftInput:
    return memos_api.MemoDraftInput(title="x", body="y", addressee_id=hotel.boss.user_id)
