"""Pydantic-схемы границ модуля feedback (R-6, spec 0076 §4.2)."""

from __future__ import annotations

import uuid
from typing import Final

from pydantic import BaseModel, ConfigDict

from hospitality.modules.feedback.models import FeedbackRating

# Предел комментария гостя (§4.1): его держат маршрут веб-чата (422) и сервис.
COMMENT_MAX_CHARS: Final = 500


class FeedbackRead(BaseModel):
    """Итог вызова `rate_request` / `add_comment` — для ответа гостю.

    `passed_to_staff` — дошло ли ЭТО действие до сотрудников: у `down` — карточка
    в службе эскалаций есть, у комментария — он записан в карточку (дописан или
    заведена новая). False у `down` — гостю нельзя говорить «передадим» и
    «передали» (запись «потом» ревью PR #914): ответ — «Спасибо за оценку!».
    У `up` всегда False — передавать нечего.
    """

    model_config = ConfigDict(frozen=True)

    request_id: uuid.UUID
    rating: FeedbackRating
    commented: bool
    passed_to_staff: bool


class FeedbackState(BaseModel):
    """Состояние виджета под итогом заявки (§5): оценки нет — `rating` None.

    `passed_to_staff` — карточка по оценке есть: после перезагрузки страница говорит
    гостю то же, что сразу после касания (`FeedbackRead.passed_to_staff`).
    """

    model_config = ConfigDict(frozen=True)

    rating: FeedbackRating | None
    commented: bool
    passed_to_staff: bool


class FeedbackCount(BaseModel):
    """Оценки одной службы за окно сводки (§6)."""

    model_config = ConfigDict(frozen=True)

    category_id: uuid.UUID
    up: int
    down: int
