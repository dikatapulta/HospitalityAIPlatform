"""Публичный интерфейс модуля memos (spec 0077 §6, §8, FOUNDATION §5.2, R-5) — копия канона
`modules/requests/api.py`.

Единственная точка входа в модуль снаружи: кабинет (`staff_portal`, PR 4 и 5б серии
#936) и ретеншн (`channels/common/retention.py`) импортируют ТОЛЬКО отсюда — остальные файлы
модуля приватны (контракт 5 import-linter). Здесь нет логики, только контракт.
Событий модуль не публикует: пуши участникам шлёт кабинет (PR 4).
"""

from __future__ import annotations

from hospitality.modules.memos.access import (
    ERR_MEMO_NO_SIGNATURE_SAMPLE,
    ERR_MEMO_NOT_FOUND,
    ERR_MEMO_WRONG_STATE,
)
from hospitality.modules.memos.deadline import explanation_due
from hospitality.modules.memos.demands import (
    begin_receipt,
    confirm_receipt,
    create_demand,
    get_demand,
    list_demands,
    read_demand_photo,
)
from hospitality.modules.memos.explanations import (
    create_explanation,
    create_supplement,
    update_explanation,
)
from hospitality.modules.memos.models import (
    MemoKind,
    MemoStatus,
    SignatureResponse,
    SignatureRole,
)
from hospitality.modules.memos.reading import get_memo, list_memos, memo_participants
from hospitality.modules.memos.retention import delete_expired_memos
from hospitality.modules.memos.schemas import (
    BODY_MAX_CHARS,
    COMMENT_MAX_CHARS,
    MAX_SIGNERS,
    REMARK_MAX_CHARS,
    TITLE_MAX_CHARS,
    DecisionIntent,
    DemandCard,
    DemandExplanation,
    DemandState,
    DemandSummary,
    ExplanationDraftInput,
    ExplanationText,
    MemoBox,
    MemoCard,
    MemoDecision,
    MemoDraftInput,
    MemoIntent,
    MemoParticipants,
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
    "DemandCard",
    "DemandExplanation",
    "DemandState",
    "DemandSummary",
    "ExplanationDraftInput",
    "ExplanationText",
    "MemoBox",
    "MemoCard",
    "MemoDecision",
    "MemoDraftInput",
    "MemoIntent",
    "MemoKind",
    "MemoParticipants",
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
    "begin_receipt",
    "complete_action",
    "confirm_receipt",
    "create_based_on",
    "create_demand",
    "create_draft",
    "create_explanation",
    "create_supplement",
    "current_signature_sample",
    "delete_expired_memos",
    "explanation_due",
    "get_demand",
    "get_memo",
    "list_demands",
    "list_memos",
    "memo_participants",
    "read_demand_photo",
    "revoke_signature_sample",
    "set_signature_sample",
    "submit_to_addressee",
    "update_draft",
    "update_explanation",
    "withdraw",
]
