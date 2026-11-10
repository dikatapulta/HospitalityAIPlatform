"""Оценка под сообщениями страницы (spec 0076 §5): poll и история несут поле `feedback`.

Сообщение итога знает свою заявку (`messages.request_id`, его пишет только
`notify_guest_on_request_closed`); что из этого можно оценить — решает домен: только
`done` этого Stay и только у отеля с включёнными оценками. У сообщений без заявки —
ни одного запроса в базу.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from hospitality.channels.web.schemas import MessageFeedback
from hospitality.modules.feedback import api as feedback_api


async def message_feedback(
    request_ids: Iterable[uuid.UUID | None], *, stay_id: uuid.UUID
) -> dict[uuid.UUID, MessageFeedback]:
    """Состояние виджета по заявкам сообщений страницы; заявка без виджета — не в словаре."""
    ids = {request_id for request_id in request_ids if request_id is not None}
    if not ids:
        return {}
    states = await feedback_api.feedback_states(ids, stay_id=stay_id)
    return {
        request_id: MessageFeedback(
            request_id=request_id,
            rating=state.rating,
            commented=state.commented,
            passed_to_staff=state.passed_to_staff,
        )
        for request_id, state in states.items()
    }
