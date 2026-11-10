"""Ход записки (spec 0077 §3, §12 PR 3): черновик → подписи → адресат → решение;
лист фиксируется отправкой; каждый лишний переход — ERR-MEMO-002."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable

import pytest

from hospitality.modules.memos import api as memos_api
from hospitality.modules.memos.tests.conftest import (
    STROKES,
    TITLE,
    Hotel,
    act,
    draft,
    sent_memo,
)
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_confirm import ConfirmationMethod, ProofStatus
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.test_staff_auth import PASSWORD

Status = memos_api.MemoStatus
SIGN = memos_api.SignIntent()


async def _code(call: Awaitable[object]) -> str:
    with pytest.raises(AppError) as error:
        await call
    return error.value.code


async def _card(hotel: Hotel, memo_id: uuid.UUID) -> memos_api.MemoCard:
    with tenant_context(hotel.tenant_id):
        return await memos_api.get_memo(hotel.author, memo_id)


async def test_memo_goes_from_draft_to_decision(hotel: Hotel) -> None:
    """Подпись, замечание, отправка руководителю и решение — в листе с подтверждением."""
    memo_id = await sent_memo(hotel)
    card = await _card(hotel, memo_id)
    assert card.status is Status.SIGNING
    assert card.number is not None and card.number.startswith("СЗ-") and card.number.endswith("/1")
    assert card.title == TITLE and card.content_intact

    assert await act(hotel, hotel.signer_a, memo_id, SIGN) is Status.SIGNING
    remark = memos_api.RemarkIntent(remark="Уточните поставщика")
    assert await act(hotel, hotel.signer_b, memo_id, remark) is Status.SIGNING
    with tenant_context(hotel.tenant_id):
        await memos_api.submit_to_addressee(hotel.author, memo_id)
    decision = memos_api.DecisionIntent(decision=memos_api.MemoDecision.APPROVED)
    assert await act(hotel, hotel.boss, memo_id, decision) is Status.APPROVED

    card = await _card(hotel, memo_id)
    sheet = {(row.user_id, row.role): row for row in card.signatures}
    author = sheet[(hotel.author.user_id, memos_api.SignatureRole.AUTHOR)]
    assert author.method is ConfirmationMethod.PASSKEY
    assert author.proof_status is ProofStatus.VERIFIED
    assert author.sample is not None and author.sample.strokes == STROKES
    signer_b = sheet[(hotel.signer_b.user_id, memos_api.SignatureRole.SIGNER)]
    assert signer_b.response is memos_api.SignatureResponse.REMARK
    assert signer_b.remark == "Уточните поставщика"
    assert signer_b.proof_status is ProofStatus.PASSWORD_ONLY
    boss = sheet[(hotel.boss.user_id, memos_api.SignatureRole.ADDRESSEE)]
    assert boss.response is memos_api.SignatureResponse.SIGNED and boss.signed_at is not None
    assert card.decided_at is not None and card.decision_comment is None


async def test_without_signers_memo_goes_straight_to_addressee(hotel: Hotel) -> None:
    memo_id = await sent_memo(hotel, signers=())
    card = await _card(hotel, memo_id)
    assert card.status is Status.SUBMITTED and card.submitted_at == card.sent_at


async def test_submit_freezes_the_signature_sheet(hotel: Hotel) -> None:
    """Не ответивший к отправке руководителю подписать уже не может (§3.4)."""
    memo_id = await sent_memo(hotel)
    await act(hotel, hotel.signer_a, memo_id, SIGN)
    with tenant_context(hotel.tenant_id):
        await memos_api.submit_to_addressee(hotel.author, memo_id)
    assert await _code(act(hotel, hotel.signer_b, memo_id, SIGN)) == memos_api.ERR_MEMO_WRONG_STATE
    card = await _card(hotel, memo_id)
    late = next(row for row in card.signatures if row.user_id == hotel.signer_b.user_id)
    assert late.response is None and late.signed_at is None


async def test_every_extra_transition_is_wrong_state(hotel: Hotel) -> None:
    """Повтор из второй вкладки и действие не по статусу — ERR-MEMO-002 (§3)."""
    wrong = memos_api.ERR_MEMO_WRONG_STATE
    approve = memos_api.DecisionIntent(decision=memos_api.MemoDecision.APPROVED)
    with tenant_context(hotel.tenant_id):
        draft_id = await memos_api.create_draft(hotel.author, draft(hotel))
        assert await _code(memos_api.submit_to_addressee(hotel.author, draft_id)) == wrong
        assert await _code(memos_api.create_based_on(hotel.author, draft_id)) == wrong
    memo_id = await sent_memo(hotel)
    with tenant_context(hotel.tenant_id):
        assert await _code(memos_api.update_draft(hotel.author, memo_id, draft(hotel))) == wrong
        assert (
            await _code(memos_api.begin_action(hotel.author, memo_id, memos_api.SendIntent()))
            == wrong
        )
        assert await _code(memos_api.begin_action(hotel.boss, memo_id, approve)) == wrong
    await act(hotel, hotel.signer_a, memo_id, SIGN)
    assert await _code(act(hotel, hotel.signer_a, memo_id, SIGN)) == wrong
    with tenant_context(hotel.tenant_id):
        await memos_api.submit_to_addressee(hotel.author, memo_id)
        assert await _code(memos_api.submit_to_addressee(hotel.author, memo_id)) == wrong
        assert await _code(memos_api.withdraw(hotel.author, memo_id)) == wrong
    await act(hotel, hotel.boss, memo_id, approve)
    assert await _code(act(hotel, hotel.boss, memo_id, approve)) == wrong


async def test_second_tab_confirmation_is_refused(hotel: Hotel) -> None:
    """Две вкладки открыли окно подтверждения подписи: вторая получает ERR-MEMO-002."""
    memo_id = await sent_memo(hotel)
    with tenant_context(hotel.tenant_id):
        first = await memos_api.begin_action(hotel.signer_a, memo_id, SIGN)
        second = await memos_api.begin_action(hotel.signer_a, memo_id, SIGN)
        for request, expected in ((first, None), (second, memos_api.ERR_MEMO_WRONG_STATE)):
            answer = memos_api.PasswordAnswer(
                challenge_id=request.challenge_id, password=PASSWORD, client_ip="ip-tab"
            )
            call = memos_api.complete_action(hotel.signer_a, memo_id, SIGN, answer)
            if expected is None:
                assert await call is Status.SIGNING
            else:
                assert await _code(call) == expected


async def test_returned_memo_is_rewritten_as_a_new_draft(hotel: Hotel) -> None:
    """«На доработку» запись не открывает: новая — с тем же текстом и ссылкой «взамен»."""
    memo_id = await sent_memo(hotel, signers=(hotel.signer_a,))
    with tenant_context(hotel.tenant_id):
        await memos_api.submit_to_addressee(hotel.author, memo_id)
    returned = memos_api.DecisionIntent(
        decision=memos_api.MemoDecision.RETURNED, comment="Добавьте смету"
    )
    assert await act(hotel, hotel.boss, memo_id, returned) is Status.RETURNED
    with tenant_context(hotel.tenant_id):
        new_id = await memos_api.create_based_on(hotel.author, memo_id)
    card = await _card(hotel, new_id)
    assert (card.status, card.based_on_id, card.number) == (Status.DRAFT, memo_id, None)
    assert card.body == (await _card(hotel, memo_id)).body
    assert [row.user_id for row in card.signatures] == [hotel.signer_a.user_id]
    assert (await _card(hotel, memo_id)).decision_comment == "Добавьте смету"


async def test_refusal_needs_a_comment() -> None:
    with pytest.raises(ValueError, match="comment"):
        memos_api.DecisionIntent(decision=memos_api.MemoDecision.REJECTED)


async def test_withdrawn_memo_keeps_its_number(hotel: Hotel) -> None:
    first = await sent_memo(hotel)
    with tenant_context(hotel.tenant_id):
        await memos_api.withdraw(hotel.author, first)
    second = await sent_memo(hotel)
    numbers = [(await _card(hotel, memo_id)).number for memo_id in (first, second)]
    assert [number.rsplit("/", 1)[1] for number in numbers if number] == ["1", "2"]
    assert (await _card(hotel, first)).status is Status.WITHDRAWN


async def test_action_without_signature_sample_does_not_start(hotel: Hotel) -> None:
    memo_id = await sent_memo(hotel)
    with tenant_context(hotel.tenant_id):
        assert await memos_api.revoke_signature_sample(hotel.signer_a.user_id)
        code = await _code(memos_api.begin_action(hotel.signer_a, memo_id, SIGN))
        assert code == memos_api.ERR_MEMO_NO_SIGNATURE_SAMPLE
        assert await memos_api.current_signature_sample(hotel.signer_a) is None
        await memos_api.set_signature_sample(hotel.signer_a, STROKES)
    assert await act(hotel, hotel.signer_a, memo_id, SIGN) is Status.SIGNING


async def test_participants_are_checked(hotel: Hotel) -> None:
    """Адресат — старший или менеджер; подписант — не автор и не адресат (§3.1)."""
    bad_addressee = draft(hotel).model_copy(update={"addressee_id": hotel.signer_a.user_id})
    self_signer = draft(hotel, signers=(hotel.author,))
    unknown_template = draft(hotel).model_copy(update={"template_key": "nope"})
    with tenant_context(hotel.tenant_id):
        for bad in (bad_addressee, self_signer, unknown_template):
            with pytest.raises(AppError) as error:
                await memos_api.create_draft(hotel.author, bad)
            assert error.value.status_code == 422
        senior_draft = draft(hotel).model_copy(update={"addressee_id": hotel.senior.user_id})
        assert await memos_api.create_draft(hotel.author, senior_draft)


async def test_boxes_show_what_waits_for_me(hotel: Hotel) -> None:
    memo_id = await sent_memo(hotel)
    with tenant_context(hotel.tenant_id):
        draft_id = await memos_api.create_draft(hotel.author, draft(hotel))

        async def ids(staff: StaffContext, box: memos_api.MemoBox) -> set[uuid.UUID]:
            return {row.id for row in await memos_api.list_memos(staff, box)}

        assert await ids(hotel.signer_a, memos_api.MemoBox.WAITING) == {memo_id}
        assert await ids(hotel.boss, memos_api.MemoBox.WAITING) == set()
        assert await ids(hotel.author, memos_api.MemoBox.MINE) == {memo_id, draft_id}
        # Черновик подписанту и адресату не виден.
        assert await ids(hotel.signer_a, memos_api.MemoBox.ALL) == {memo_id}
        assert await ids(hotel.boss, memos_api.MemoBox.ALL) == {memo_id}
        await memos_api.submit_to_addressee(hotel.author, memo_id)
        assert await ids(hotel.signer_a, memos_api.MemoBox.WAITING) == set()
        assert await ids(hotel.boss, memos_api.MemoBox.WAITING) == {memo_id}
        titles = [
            row.title for row in await memos_api.list_memos(hotel.boss, memos_api.MemoBox.ALL)
        ]
        assert titles == [TITLE]


async def test_opening_a_sent_memo_is_logged_for_participants(hotel: Hotel) -> None:
    memo_id = await sent_memo(hotel)
    with tenant_context(hotel.tenant_id):
        await memos_api.get_memo(hotel.signer_a, memo_id)
        card = await memos_api.get_memo(hotel.boss, memo_id)
    assert [view.user_id for view in card.views] == [hotel.signer_a.user_id, hotel.boss.user_id]
