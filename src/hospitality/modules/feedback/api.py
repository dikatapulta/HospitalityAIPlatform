"""Публичный интерфейс модуля feedback (spec 0076 §4.2, FOUNDATION §5.2, R-5) —
копия канона `modules/requests/api.py`.

Единственная точка входа в модуль снаружи: веб-чат (`channels/web`, PR 2 серии),
ретеншн (`channels/common/retention.py`), кабинет (`staff_portal/summary.py`) импортируют ТОЛЬКО
отсюда — остальные файлы модуля приватны (контракт 5 import-linter). Здесь нет
логики, только контракт. Событий модуль не публикует: подписчиков нет.
"""

from __future__ import annotations

from hospitality.modules.feedback.models import FeedbackRating
from hospitality.modules.feedback.schemas import (
    COMMENT_MAX_CHARS,
    FeedbackCount,
    FeedbackRead,
    FeedbackState,
)
from hospitality.modules.feedback.service import (
    ERR_FEEDBACK_ALREADY_RATED,
    ERR_FEEDBACK_CARD_FAILED,
    ERR_FEEDBACK_COMMENT_NOT_APPLICABLE,
    ERR_FEEDBACK_DISABLED,
    ERR_FEEDBACK_NOT_RATEABLE,
    add_comment,
    clear_expired_comments,
    count_feedback,
    feedback_states,
    rate_request,
)

__all__ = [
    "COMMENT_MAX_CHARS",
    "ERR_FEEDBACK_ALREADY_RATED",
    "ERR_FEEDBACK_CARD_FAILED",
    "ERR_FEEDBACK_COMMENT_NOT_APPLICABLE",
    "ERR_FEEDBACK_DISABLED",
    "ERR_FEEDBACK_NOT_RATEABLE",
    "FeedbackCount",
    "FeedbackRating",
    "FeedbackRead",
    "FeedbackState",
    "add_comment",
    "clear_expired_comments",
    "count_feedback",
    "feedback_states",
    "rate_request",
]
