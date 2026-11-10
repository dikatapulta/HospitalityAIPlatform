"""Шифртексты строк модуля memos ↔ значения, хэш содержимого, предмет подтверждения.

Все `*_ciphertext` — через канон `platform/field_encryption.py` с AAD
`{tenant_id}/{таблица}/{id строки}/{колонка}`: шифртекст, переложенный в чужую
строку, не расшифруется. Здесь — единственное место, где модуль знает формат
шифруемых значений и состав подтверждаемого (spec 0077 §4).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final

from hospitality.modules.memos.models import Memo, MemoSignature, MemoSignatureSample
from hospitality.modules.memos.schemas import SignatureStrokes
from hospitality.platform.field_encryption import decrypt_field, encrypt_field, field_aad
from hospitality.platform.staff_confirm import (
    ConfirmationProof,
    ConfirmationSubject,
    content_sha256,
)

# Действия под подтверждением — `ConfirmationSubject.action`.
ACTION_SEND: Final = "memo_send"
ACTION_SIGN: Final = "memo_sign"
ACTION_REMARK: Final = "memo_remark"
ACTION_DECIDE: Final = "memo_decide"


def _seal(tenant_id: uuid.UUID, table: str, row_id: uuid.UUID, column: str, value: str) -> bytes:
    return encrypt_field(value, field_aad(tenant_id, table, row_id, column))


def _open(tenant_id: uuid.UUID, table: str, row_id: uuid.UUID, column: str, blob: bytes) -> str:
    return decrypt_field(blob, field_aad(tenant_id, table, row_id, column))


def seal_memo(memo: Memo, column: str, value: str) -> bytes:
    return _seal(memo.tenant_id, Memo.__tablename__, memo.id, column, value)


def open_memo(memo: Memo, column: str, blob: bytes) -> str:
    return _open(memo.tenant_id, Memo.__tablename__, memo.id, column, blob)


def memo_title(memo: Memo) -> str:
    return open_memo(memo, "title_ciphertext", memo.title_ciphertext)


def memo_body(memo: Memo) -> str:
    return open_memo(memo, "body_ciphertext", memo.body_ciphertext)


def decision_comment(memo: Memo) -> str | None:
    blob = memo.decision_comment_ciphertext
    return None if blob is None else open_memo(memo, "decision_comment_ciphertext", blob)


def stored_content_hash(memo: Memo) -> str | None:
    blob = memo.content_hash_ciphertext
    return None if blob is None else open_memo(memo, "content_hash_ciphertext", blob)


def content_hash(memo: Memo, signer_ids: Iterable[uuid.UUID]) -> str:
    """SHA-256 неизменного после отправки (§3.2): вид, шаблон, заголовок, текст, адресат,
    подписанты, «взамен». Номера нет — он выдаётся самой отправкой."""
    return content_sha256(
        memo.kind.value,
        memo.template_key or "",
        memo_title(memo),
        memo_body(memo),
        str(memo.addressee_id),
        str(memo.based_on_id or ""),
        *sorted(str(signer_id) for signer_id in signer_ids),
    )


def subject(memo: Memo, action: str, memo_hash: str, *extra: str) -> ConfirmationSubject:
    """Что подтверждается: у замечания и решения — ещё их текст (§4)."""
    return ConfirmationSubject(
        tenant_id=memo.tenant_id,
        record_id=memo.id,
        action=action,
        content_sha256=content_sha256(memo_hash, *extra) if extra else memo_hash,
    )


def seal_signature(row: MemoSignature, column: str, value: str) -> bytes:
    return _seal(row.tenant_id, MemoSignature.__tablename__, row.id, column, value)


def remark(row: MemoSignature) -> str | None:
    blob = row.remark_ciphertext
    if blob is None:
        return None
    return _open(row.tenant_id, MemoSignature.__tablename__, row.id, "remark_ciphertext", blob)


def seal_proof(row: MemoSignature, proof: ConfirmationProof) -> bytes:
    return seal_signature(row, "proof_ciphertext", proof.model_dump_json())


def proof(row: MemoSignature) -> ConfirmationProof | None:
    blob = row.proof_ciphertext
    if blob is None:
        return None
    raw = _open(row.tenant_id, MemoSignature.__tablename__, row.id, "proof_ciphertext", blob)
    return ConfirmationProof.model_validate_json(raw)


def seal_strokes(row: MemoSignatureSample, strokes: SignatureStrokes) -> bytes:
    return _seal(
        row.tenant_id,
        MemoSignatureSample.__tablename__,
        row.id,
        "strokes_ciphertext",
        strokes.model_dump_json(),
    )


def strokes(row: MemoSignatureSample) -> SignatureStrokes:
    raw = _open(
        row.tenant_id,
        MemoSignatureSample.__tablename__,
        row.id,
        "strokes_ciphertext",
        row.strokes_ciphertext,
    )
    return SignatureStrokes.model_validate_json(raw)
