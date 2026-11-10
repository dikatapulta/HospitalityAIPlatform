"""Pydantic-границы маршрутов оценки (R-6, spec 0076 §5).

Stay в теле нет: маршрут берёт его только из сессии гостя. Лишние поля тела
отбрасываются — подложенный `stay_id` ничего не меняет.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, StringConstraints

from hospitality.modules.feedback import api as feedback_api

# Пустой (после обрезки по краям) или длиннее предела — 422 на границе, до домена:
# домен на такой текст бросает ValueError, и без этой схемы гость получил бы 500.
Comment = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=feedback_api.COMMENT_MAX_CHARS
    ),
]


class RateBody(BaseModel):
    """Касание пальца под итогом заявки."""

    request_id: uuid.UUID
    rating: feedback_api.FeedbackRating


class CommentBody(BaseModel):
    """«Что было не так» после пальца вниз — один раз."""

    request_id: uuid.UUID
    comment: Comment
