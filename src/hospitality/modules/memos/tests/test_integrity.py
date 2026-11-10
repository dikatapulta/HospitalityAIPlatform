"""Целостность и видимость записи (spec 0077 §4, §8, §12 PR 3): в строке — шифртекст;
правка текста в базе ломает проверку доказательства; чужому — ERR-MEMO-001, другому
отелю — ничего (RLS)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable

import pytest
from sqlalchemy import func, select, text

from hospitality.modules.memos import api as memos_api
from hospitality.modules.memos.codec import content_hash, seal_memo
from hospitality.modules.memos.models import Memo
from hospitality.modules.memos.tests.conftest import (
    BODY,
    TITLE,
    Hotel,
    act,
    draft,
    make_hotel,
    sent_memo,
)
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_confirm import ProofStatus
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

SIGN = memos_api.SignIntent()


async def _code(call: Awaitable[object]) -> str:
    with pytest.raises(AppError) as error:
        await call
    return error.value.code


async def test_editing_text_in_database_breaks_the_proof(hotel: Hotel) -> None:
    """Подпись ключом не сходится с правленым текстом; подпись паролем так и говорит —
    `password_only`, а не «сходится»; карточка видит, что текст не тот, что при отправке."""
    memo_id = await sent_memo(hotel)
    await act(hotel, hotel.signer_a, memo_id, SIGN)
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            memo = await session.get(Memo, memo_id)
            assert memo is not None
            memo.body_ciphertext = seal_memo(memo, "body_ciphertext", BODY + " И ещё 100 штук.")
        card = await memos_api.get_memo(hotel.author, memo_id)
    statuses = {row.user_id: row.proof_status for row in card.signatures}
    assert statuses[hotel.author.user_id] is ProofStatus.MISMATCH
    assert statuses[hotel.signer_a.user_id] is ProofStatus.PASSWORD_ONLY
    assert statuses[hotel.signer_b.user_id] is None
    assert card.content_intact is False


async def test_row_holds_only_ciphertext(hotel: Hotel) -> None:
    """Заголовок, текст, замечание, хэш, доказательство и штрихи — шифртекст (§8): по копии
    базы не прочесть текст и не проверить догадку о нём хэшем (ревью PR #938)."""
    memo_id = await sent_memo(hotel)
    await act(hotel, hotel.signer_a, memo_id, memos_api.RemarkIntent(remark="Замечание LEAK"))
    with tenant_context(hotel.tenant_id):
        async with session_scope() as session:
            memo = await session.get(Memo, memo_id)
            assert memo is not None
            memo_hash = content_hash(memo, [hotel.signer_a.user_id, hotel.signer_b.user_id])
            blobs = (
                await session.execute(
                    text(
                        "SELECT title_ciphertext, body_ciphertext, content_hash_ciphertext "
                        "FROM memos UNION ALL "
                        "SELECT remark_ciphertext, proof_ciphertext, NULL FROM memo_signatures "
                        "UNION ALL "
                        "SELECT strokes_ciphertext, NULL, NULL FROM memo_signature_samples"
                    )
                )
            ).all()
    stored = b"".join(blob for row in blobs for blob in row if blob is not None)
    for plain in (TITLE, BODY[:20], "LEAK", memo_hash, '"method"', '"strokes"', "[10,10]"):
        assert plain.encode() not in stored


async def _forbidden_code(staff: StaffContext, memo_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    with tenant_context(tenant_id):
        return await _code(memos_api.get_memo(staff, memo_id))


async def test_strangers_get_not_found(hotel: Hotel) -> None:
    """Постороннему, менеджеру-неучастнику и суперадмину — ERR-MEMO-001, как несуществующей."""
    memo_id = await sent_memo(hotel)
    superadmin = hotel.other_manager.model_copy(
        update={"user_id": uuid.uuid4(), "is_platform_admin": True}
    )
    for stranger in (hotel.outsider, hotel.other_manager, superadmin):
        code = await _forbidden_code(stranger, memo_id, hotel.tenant_id)
        assert code == memos_api.ERR_MEMO_NOT_FOUND
    with tenant_context(hotel.tenant_id):
        for box in memos_api.MemoBox:
            assert await memos_api.list_memos(hotel.other_manager, box) == []
        code = await _code(memos_api.begin_action(hotel.outsider, memo_id, SIGN))
        assert code == memos_api.ERR_MEMO_NOT_FOUND
        decide = memos_api.DecisionIntent(decision=memos_api.MemoDecision.APPROVED)
        code = await _code(memos_api.begin_action(hotel.signer_a, memo_id, decide))
        assert code == memos_api.ERR_MEMO_NOT_FOUND


async def test_draft_is_seen_only_by_its_author(hotel: Hotel) -> None:
    with tenant_context(hotel.tenant_id):
        draft_id = await memos_api.create_draft(hotel.author, draft(hotel))
        assert (await memos_api.get_memo(hotel.author, draft_id)).views == []
    for participant in (hotel.signer_a, hotel.boss):
        code = await _forbidden_code(participant, draft_id, hotel.tenant_id)
        assert code == memos_api.ERR_MEMO_NOT_FOUND


async def test_draft_withdrawn_before_sending_stays_authors(hotel: Hotel) -> None:
    """Ревью PR #946: отозванный до отправки черновик — `withdrawn`, но не отправлен:
    подписанту и адресату — ERR-MEMO-001, во вкладке «Все» его нет, просмотр не пишется."""
    with tenant_context(hotel.tenant_id):
        draft_id = await memos_api.create_draft(hotel.author, draft(hotel))
        await memos_api.withdraw(hotel.author, draft_id)
        card = await memos_api.get_memo(hotel.author, draft_id)
        assert card.status is memos_api.MemoStatus.WITHDRAWN and card.views == []
        for participant in (hotel.signer_a, hotel.boss):
            listed = await memos_api.list_memos(participant, memos_api.MemoBox.ALL)
            assert draft_id not in {memo.id for memo in listed}
    for participant in (hotel.signer_a, hotel.boss):
        code = await _forbidden_code(participant, draft_id, hotel.tenant_id)
        assert code == memos_api.ERR_MEMO_NOT_FOUND


async def test_other_hotel_sees_nothing(hotel: Hotel) -> None:
    """RLS (P-4): менеджер другого отеля не находит записку и строк её таблиц не видит."""
    memo_id = await sent_memo(hotel)
    other = await make_hotel("hotel-memos-b")
    assert other.boss.role_key is StaffRole.MANAGER
    assert await _forbidden_code(other.boss, memo_id, other.tenant_id) == "ERR-MEMO-001"
    with tenant_context(other.tenant_id):
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(Memo)) == 0
            for table in ("memo_signatures", "memo_views", "memo_counters"):
                count = await session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                assert count == 0
