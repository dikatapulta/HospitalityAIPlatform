"""Действия под именем — отправка, подпись, замечание, решение — и образец подписи
(spec 0077 §3, §4).

Два шага, как окно подтверждения в браузере: `begin_action` описывает действие
(`ConfirmationSubject`: отель, запись, действие, SHA-256 содержимого, у замечания и
решения — ещё их текст) и открывает вызов `staff_confirm`; `complete_action`
принимает ответ ключа или пароль, получает доказательство и под блокировкой записи
сверяет, что запись не изменилась, — иначе ERR-MEMO-002 (вторая вкладка, чужой
переход). Доказательство ложится шифртекстом в строку листа. Без образца подписи
действие не начинается — ERR-MEMO-003.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.memos.access import (
    check_explanation_addressee,
    check_participants,
    load_visible,
    no_signature_sample,
    not_found,
    require_author,
    signer_ids,
    wrong_state,
)
from hospitality.modules.memos.codec import (
    ACTION_DECIDE,
    ACTION_REMARK,
    ACTION_SEND,
    ACTION_SIGN,
    content_hash,
    seal_memo,
    seal_proof,
    seal_signature,
    seal_strokes,
    strokes,
    subject,
)
from hospitality.modules.memos.models import (
    Memo,
    MemoKind,
    MemoSignature,
    MemoSignatureSample,
    MemoStatus,
    SignatureResponse,
    SignatureRole,
)
from hospitality.modules.memos.schemas import (
    DecisionIntent,
    MemoIntent,
    PasskeyAnswer,
    PasswordAnswer,
    RemarkIntent,
    SendIntent,
    SignatureSampleRead,
    SignatureStrokes,
)
from hospitality.modules.memos.service import hotel_config, next_number
from hospitality.platform.staff_confirm import (
    ConfirmationProof,
    ConfirmationRequest,
    ConfirmationSubject,
    begin_confirmation,
    confirm_with_passkey,
    confirm_with_password,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)


@dataclass(frozen=True)
class _Prepared:
    subject: ConfirmationSubject
    memo_hash: str
    own_row: MemoSignature | None  # строка подписанта; у автора и адресата — None


def _prepare(
    memo: Memo, rows: list[MemoSignature], staff: StaffContext, intent: MemoIntent
) -> _Prepared:
    """Можно ли этому сотруднику это действие сейчас, и что он подтверждает."""
    memo_hash = content_hash(memo, signer_ids(rows))
    if isinstance(intent, SendIntent):
        require_author(memo, staff, MemoStatus.DRAFT)
        return _Prepared(subject(memo, ACTION_SEND, memo_hash), memo_hash, None)
    if isinstance(intent, DecisionIntent):
        # У объяснительной решения нет (§6): кнопки нет ни у кого, как чужой записи.
        if memo.addressee_id != staff.user_id or memo.kind is MemoKind.EXPLANATION:
            raise not_found()
        if memo.status is not MemoStatus.SUBMITTED:
            raise wrong_state()
        decided = subject(memo, ACTION_DECIDE, memo_hash, intent.decision.value, intent.comment)
        return _Prepared(decided, memo_hash, None)
    own = next(
        (r for r in rows if r.user_id == staff.user_id and r.role is SignatureRole.SIGNER), None
    )
    if own is None:
        raise not_found()
    if memo.status is not MemoStatus.SIGNING or own.response is not None:
        raise wrong_state()
    if isinstance(intent, RemarkIntent):
        return _Prepared(subject(memo, ACTION_REMARK, memo_hash, intent.remark), memo_hash, own)
    return _Prepared(subject(memo, ACTION_SIGN, memo_hash), memo_hash, own)


async def current_sample(session: AsyncSession, user_id: uuid.UUID) -> MemoSignatureSample | None:
    sample: MemoSignatureSample | None = await session.scalar(
        select(MemoSignatureSample).where(
            MemoSignatureSample.user_id == user_id, MemoSignatureSample.revoked_at.is_(None)
        )
    )
    return sample


async def begin_action(
    staff: StaffContext, memo_id: uuid.UUID, intent: MemoIntent
) -> ConfirmationRequest:
    """Открыть окно подтверждения действия: проверки статуса и образца — до вызова."""
    async with session_scope() as session:
        memo, rows = await load_visible(session, staff, memo_id)
        prepared = _prepare(memo, rows, staff, intent)
        if await current_sample(session, staff.user_id) is None:
            raise no_signature_sample()
    if isinstance(intent, SendIntent):
        await _check_send(staff, memo, rows)
    return await begin_confirmation(staff, prepared.subject)


async def _check_send(staff: StaffContext, memo: Memo, rows: list[MemoSignature]) -> None:
    """Участники перед отправкой: за время черновика кто-то мог уйти из отеля."""
    if memo.kind is MemoKind.EXPLANATION:
        await check_explanation_addressee(staff, memo)
    else:
        await check_participants(staff, memo.addressee_id, signer_ids(rows))


async def confirm_answer(
    staff: StaffContext, confirmed: ConfirmationSubject, answer: PasskeyAnswer | PasswordAnswer
) -> ConfirmationProof:
    if isinstance(answer, PasskeyAnswer):
        return await confirm_with_passkey(
            staff,
            confirmed,
            challenge_id=answer.challenge_id,
            credential_json=answer.credential_json,
        )
    return await confirm_with_password(
        staff,
        confirmed,
        challenge_id=answer.challenge_id,
        password=answer.password,
        client_ip=answer.client_ip,
        device_token=answer.device_token,
    )


async def complete_action(
    staff: StaffContext,
    memo_id: uuid.UUID,
    intent: MemoIntent,
    answer: PasskeyAnswer | PasswordAnswer,
) -> MemoStatus:
    """Принять подтверждение и выполнить действие; вернуть новый статус записи.

    Отказ подтверждения — ERR-AUTH-017 (ERR-AUTH-006 — бюджет пароля); запись за это
    время изменилась — ERR-MEMO-002, подтверждение при этом израсходовано."""
    async with session_scope() as session:
        memo, rows = await load_visible(session, staff, memo_id)
        prepared = _prepare(memo, rows, staff, intent)
    if isinstance(intent, SendIntent):
        await _check_send(staff, memo, rows)
    proof = await confirm_answer(staff, prepared.subject, answer)
    local_year = utc_now().astimezone((await hotel_config(staff.tenant_id)).tzinfo).year
    async with session_scope() as session:
        memo, rows = await load_visible(session, staff, memo_id, lock=True)
        again = _prepare(memo, rows, staff, intent)
        if again.subject != prepared.subject:
            raise wrong_state()
        sample = await current_sample(session, staff.user_id)
        if sample is None:
            raise no_signature_sample()
        now = utc_now()
        if isinstance(intent, SendIntent):
            memo.number = await next_number(session, memo.tenant_id, memo.kind, local_year)
            memo.content_hash_ciphertext = seal_memo(
                memo, "content_hash_ciphertext", again.memo_hash
            )
            has_signers = bool(signer_ids(rows))
            memo.status = MemoStatus.SIGNING if has_signers else MemoStatus.SUBMITTED
            memo.sent_at = now
            memo.submitted_at = None if has_signers else now
            row = MemoSignature(
                id=uuid.uuid4(),
                tenant_id=memo.tenant_id,
                memo_id=memo.id,
                user_id=staff.user_id,
                role=SignatureRole.AUTHOR,
            )
            session.add(row)
        elif isinstance(intent, DecisionIntent):
            memo.status = MemoStatus(intent.decision.value)
            memo.decided_at = now
            if intent.comment:
                memo.decision_comment_ciphertext = seal_memo(
                    memo, "decision_comment_ciphertext", intent.comment
                )
            row = MemoSignature(
                id=uuid.uuid4(),
                tenant_id=memo.tenant_id,
                memo_id=memo.id,
                user_id=staff.user_id,
                role=SignatureRole.ADDRESSEE,
            )
            session.add(row)
        else:
            assert again.own_row is not None  # _prepare гарантирует у подписанта
            row = again.own_row
        _sign_row(row, intent, proof, sample.id)
        memo.updated_at = now
        status = memo.status
    logger.info(
        "memo_action_confirmed",
        memo_id=str(memo_id),
        action=prepared.subject.action,
        method=proof.method.value,
        status=status.value,
    )
    return status


def _sign_row(
    row: MemoSignature,
    intent: MemoIntent,
    proof: ConfirmationProof,
    sample_id: uuid.UUID,
) -> None:
    is_remark = isinstance(intent, RemarkIntent)
    row.response = SignatureResponse.REMARK if is_remark else SignatureResponse.SIGNED
    if isinstance(intent, RemarkIntent):
        row.remark_ciphertext = seal_signature(row, "remark_ciphertext", intent.remark)
    row.method = proof.method.value
    row.proof_ciphertext = seal_proof(row, proof)
    row.sample_id = sample_id
    row.signed_at = proof.confirmed_at


async def set_signature_sample(staff: StaffContext, sample: SignatureStrokes) -> uuid.UUID:
    """Новый образец подписи (§4): прежний гасится и живёт, пока на него ссылается лист."""
    async with session_scope() as session:
        await session.execute(
            update(MemoSignatureSample)
            .where(
                MemoSignatureSample.user_id == staff.user_id,
                MemoSignatureSample.revoked_at.is_(None),
            )
            .values(revoked_at=utc_now())
        )
        row = MemoSignatureSample(id=uuid.uuid4(), tenant_id=staff.tenant_id, user_id=staff.user_id)
        row.strokes_ciphertext = seal_strokes(row, sample)
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:  # второй образец той же секунды из другой вкладки
            raise wrong_state() from exc
    logger.info("memo_signature_sample_set", sample_id=str(row.id))
    return row.id


async def current_signature_sample(staff: StaffContext) -> SignatureSampleRead | None:
    async with session_scope() as session:
        row = await current_sample(session, staff.user_id)
        if row is None:
            return None
        return SignatureSampleRead(id=row.id, strokes=strokes(row), created_at=row.created_at)


async def revoke_signature_sample(user_id: uuid.UUID) -> bool:
    """«Сбросить подпись» (§4): образец сотрудника гаснет; права проверяет кабинет (PR 4)."""
    async with session_scope() as session:
        result = await session.execute(
            update(MemoSignatureSample)
            .where(
                MemoSignatureSample.user_id == user_id,
                MemoSignatureSample.revoked_at.is_(None),
            )
            .values(revoked_at=utc_now())
            .returning(MemoSignatureSample.id)
        )
        revoked = result.first() is not None
    logger.info("memo_signature_sample_revoked", user_id=str(user_id), revoked=revoked)
    return revoked
