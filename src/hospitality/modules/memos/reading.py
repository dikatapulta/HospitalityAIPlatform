"""Карточка и реестр записей (spec 0077 §5): только участнику, тексты расшифрованы.

Карточка проверяет каждое доказательство против нынешнего содержимого
(`staff_confirm.verify_proof`): текст, правленый в базе мимо приложения, даёт
`mismatch` у подписей ключом и `content_intact = False`. Открытие отправленной
записи пишет строку журнала просмотров.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ColumnElement, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.memos.access import load_visible, signer_ids
from hospitality.modules.memos.codec import (
    ACTION_DECIDE,
    ACTION_REMARK,
    ACTION_SEND,
    ACTION_SIGN,
    content_hash,
    decision_comment,
    memo_body,
    memo_title,
    proof,
    remark,
    stored_content_hash,
    strokes,
    subject,
)
from hospitality.modules.memos.models import (
    Memo,
    MemoSignature,
    MemoSignatureSample,
    MemoStatus,
    MemoView,
    SignatureResponse,
    SignatureRole,
)
from hospitality.modules.memos.schemas import (
    MemoBox,
    MemoCard,
    MemoParticipants,
    MemoSummary,
    SignatureRead,
    SignatureSampleRead,
    ViewRead,
)
from hospitality.platform.staff_confirm import (
    ConfirmationMethod,
    ConfirmationProof,
    ConfirmationSubject,
    ProofStatus,
    verify_proof,
)
from hospitality.platform.staff_passkey_history import PasskeyHistory, passkey_history
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)


def _summary_fields(memo: Memo) -> dict[str, object]:
    return {
        "id": memo.id,
        "kind": memo.kind,
        "number": memo.number,
        "status": memo.status,
        "title": memo_title(memo),
        "author_id": memo.author_id,
        "addressee_id": memo.addressee_id,
        "created_at": memo.created_at,
        "sent_at": memo.sent_at,
        "updated_at": memo.updated_at,
    }


def _signed_subject(memo: Memo, row: MemoSignature, memo_hash: str) -> ConfirmationSubject:
    """Что подтверждала строка — по её роли и ответу (как в `signing._prepare`)."""
    if row.role is SignatureRole.AUTHOR:
        return subject(memo, ACTION_SEND, memo_hash)
    if row.role is SignatureRole.ADDRESSEE:
        comment = decision_comment(memo) or ""
        return subject(memo, ACTION_DECIDE, memo_hash, memo.status.value, comment)
    if row.response is SignatureResponse.REMARK:
        return subject(memo, ACTION_REMARK, memo_hash, remark(row) or "")
    return subject(memo, ACTION_SIGN, memo_hash)


async def _samples(
    session: AsyncSession, rows: list[MemoSignature]
) -> dict[uuid.UUID, SignatureSampleRead]:
    ids = [row.sample_id for row in rows if row.sample_id is not None]
    if not ids:
        return {}
    samples = await session.scalars(
        select(MemoSignatureSample).where(MemoSignatureSample.id.in_(ids))
    )
    return {
        sample.id: SignatureSampleRead(
            id=sample.id, strokes=strokes(sample), created_at=sample.created_at
        )
        for sample in samples
    }


async def _signature(
    memo: Memo,
    row: MemoSignature,
    stored: ConfirmationProof | None,
    memo_hash: str,
    samples: dict[uuid.UUID, SignatureSampleRead],
    keys: dict[uuid.UUID, PasskeyHistory],
) -> SignatureRead:
    status: ProofStatus | None = None
    if stored is not None:
        status = await verify_proof(
            _signed_subject(memo, row, memo_hash), stored, signer_user_id=row.user_id
        )
    key = keys.get(stored.passkey_id) if stored and stored.passkey_id else None
    return SignatureRead(
        user_id=row.user_id,
        role=row.role,
        response=row.response,
        remark=remark(row),
        method=ConfirmationMethod(row.method) if row.method else None,
        signed_at=row.signed_at,
        sample=samples.get(row.sample_id) if row.sample_id else None,
        proof_status=status,
        password_reset_at=stored.password_reset_at if stored else None,
        key_reset_revoked_at=key.reset_revoked_at if key else None,
        key_bound_after_reset_at=key.bound_after_reset_at if key else None,
    )


async def get_memo(staff: StaffContext, memo_id: uuid.UUID) -> MemoCard:
    """Карточка записи участнику; иначе ERR-MEMO-001. Открытие отправленной — в журнал."""
    async with session_scope() as session:
        memo, rows = await load_visible(session, staff, memo_id)
        if memo.sent_at is not None:
            session.add(MemoView(tenant_id=memo.tenant_id, memo_id=memo.id, user_id=staff.user_id))
            await session.flush()
        memo_hash = content_hash(memo, signer_ids(rows))
        stored_hash = stored_content_hash(memo)
        samples = await _samples(session, rows)
        views = await session.scalars(
            select(MemoView).where(MemoView.memo_id == memo.id).order_by(MemoView.viewed_at)
        )
        view_rows = [ViewRead(user_id=view.user_id, viewed_at=view.viewed_at) for view in views]
        based_on_number = (
            await session.scalar(select(Memo.number).where(Memo.id == memo.based_on_id))
            if memo.based_on_id is not None
            else None
        )
        fields = _summary_fields(memo)
        body = memo_body(memo)
        comment = decision_comment(memo)
    proofs = [proof(row) for row in rows]
    keys = await passkey_history(
        [stored.passkey_id for stored in proofs if stored and stored.passkey_id]
    )
    signatures = [
        await _signature(memo, row, stored, memo_hash, samples, keys)
        for row, stored in zip(rows, proofs, strict=True)
    ]
    intact = stored_hash is None or stored_hash == memo_hash
    if not intact:
        logger.warning("memo_content_changed_after_send", memo_id=str(memo.id))
    return MemoCard.model_validate(
        {
            **fields,
            "template_key": memo.template_key,
            "body": body,
            "based_on_id": memo.based_on_id,
            "based_on_number": based_on_number,
            "submitted_at": memo.submitted_at,
            "decided_at": memo.decided_at,
            "decision_comment": comment,
            "signatures": signatures,
            "views": view_rows,
            "content_intact": intact,
        }
    )


def _box_filter(box: MemoBox, user_id: uuid.UUID) -> ColumnElement[bool]:
    own_signer = exists().where(
        MemoSignature.memo_id == Memo.id,
        MemoSignature.user_id == user_id,
        MemoSignature.role == SignatureRole.SIGNER,
    )
    if box is MemoBox.MINE:
        return Memo.author_id == user_id
    if box is MemoBox.WAITING:
        waiting_signer = exists().where(
            MemoSignature.memo_id == Memo.id,
            MemoSignature.user_id == user_id,
            MemoSignature.role == SignatureRole.SIGNER,
            MemoSignature.response.is_(None),
        )
        return or_(
            (Memo.status == MemoStatus.SIGNING) & waiting_signer,
            (Memo.status == MemoStatus.SUBMITTED) & (Memo.addressee_id == user_id),
        )
    return or_(
        Memo.author_id == user_id,
        Memo.sent_at.is_not(None) & or_(Memo.addressee_id == user_id, own_signer),
    )


async def list_memos(staff: StaffContext, box: MemoBox) -> list[MemoSummary]:
    """Вкладка реестра: «Ждут меня», «Мои» или «Все» — свежие сверху."""
    async with session_scope() as session:
        memos = await session.scalars(
            select(Memo).where(_box_filter(box, staff.user_id)).order_by(Memo.updated_at.desc())
        )
        return [MemoSummary.model_validate(_summary_fields(memo)) for memo in memos]


async def memo_participants(staff: StaffContext, memo_id: uuid.UUID) -> MemoParticipants:
    """Участники записи — только участнику (ERR-MEMO-001); строки просмотра не пишет."""
    async with session_scope() as session:
        memo, rows = await load_visible(session, staff, memo_id)
        return MemoParticipants(
            id=memo.id,
            status=memo.status,
            author_id=memo.author_id,
            addressee_id=memo.addressee_id,
            signer_ids=signer_ids(rows),
        )
