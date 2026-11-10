"""Публичный интерфейс модуля memos (spec 0077 §8, FOUNDATION §5.2, R-5) — копия канона
`modules/requests/api.py`.

Единственная точка входа в модуль снаружи: кабинет (`staff_portal`, PR 4 серии #936)
и ретеншн (`channels/common/retention.py`) импортируют ТОЛЬКО отсюда — остальные файлы
модуля приватны (контракт 5 import-linter). Здесь нет логики, только контракт.
Событий модуль не публикует: пуши участникам шлёт кабинет (PR 4).
"""

from __future__ import annotations

from hospitality.modules.memos.access import (
    ERR_MEMO_NO_SIGNATURE_SAMPLE,
    ERR_MEMO_NOT_FOUND,
    ERR_MEMO_WRONG_STATE,
)
from hospitality.modules.memos.models import (
    MemoKind,
    MemoStatus,
    SignatureResponse,
    SignatureRole,
)
from hospitality.modules.memos.reading import get_memo, list_memos
from hospitality.modules.memos.retention import delete_expired_memos
from hospitality.modules.memos.schemas import (
    BODY_MAX_CHARS,
    COMMENT_MAX_CHARS,
    MAX_SIGNERS,
    REMARK_MAX_CHARS,
    TITLE_MAX_CHARS,
    DecisionIntent,
    MemoBox,
    MemoCard,
    MemoDecision,
    MemoDraftInput,
    MemoIntent,
    MemoSummary,
    PasskeyAnswer,
    PasswordAnswer,
    RemarkIntent,
    SendIntent,
    SignatureRead,
    SignatureSampleRead,
    SignatureStrokes,
    SignIntent,
    ViewRead,
)
from hospitality.modules.memos.service import (
    create_based_on,
    create_draft,
    submit_to_addressee,
    update_draft,
    withdraw,
)
from hospitality.modules.memos.signing import (
    begin_action,
    complete_action,
    current_signature_sample,
    revoke_signature_sample,
    set_signature_sample,
)

__all__ = [
    "BODY_MAX_CHARS",
    "COMMENT_MAX_CHARS",
    "ERR_MEMO_NOT_FOUND",
    "ERR_MEMO_NO_SIGNATURE_SAMPLE",
    "ERR_MEMO_WRONG_STATE",
    "MAX_SIGNERS",
    "REMARK_MAX_CHARS",
    "TITLE_MAX_CHARS",
    "DecisionIntent",
    "MemoBox",
    "MemoCard",
    "MemoDecision",
    "MemoDraftInput",
    "MemoIntent",
    "MemoKind",
    "MemoStatus",
    "MemoSummary",
    "PasskeyAnswer",
    "PasswordAnswer",
    "RemarkIntent",
    "SendIntent",
    "SignIntent",
    "SignatureRead",
    "SignatureResponse",
    "SignatureRole",
    "SignatureSampleRead",
    "SignatureStrokes",
    "ViewRead",
    "begin_action",
    "complete_action",
    "create_based_on",
    "create_draft",
    "current_signature_sample",
    "delete_expired_memos",
    "get_memo",
    "list_memos",
    "revoke_signature_sample",
    "set_signature_sample",
    "submit_to_addressee",
    "update_draft",
    "withdraw",
]
