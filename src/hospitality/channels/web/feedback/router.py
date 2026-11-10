"""HTTP-маршруты оценки выполненной заявки (spec 0076 §5, PR 2 серии) — канон `table/router.py`.

Под `/g/{tenant_slug}/{room_number}`, как чат: контекст тенанта канал ставит по slug, сессия —
та же cookie `guest_session` (`router.require_guest_session`; без неё — 401), Stay — только
из сессии, `{room}` пути не используется. Тела — только JSON: SameSite=Strict cookie
отрезает чужие сайты. Отдельного лимитера нет: одна оценка и один комментарий на заявку,
повтор — прежний ответ или 409 (§5). Всё остальное решает домен (`modules/feedback`):
оценки выключены — 404 ERR-FEEDBACK-001, заявку нельзя оценить — 404 -002, оценена
иначе — 409 -003, комментарий неприменим — 409 -004.

Здесь же — файл виджета `/g/static/feedback.js` (`widget.py`), общий для всех отелей:
поэтому роутер подключён в `app.py` раньше чата, иначе путь съела бы страница
`/{tenant_slug}/{room_number}`.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response

from hospitality.channels.web.feedback import widget
from hospitality.channels.web.feedback.schemas import CommentBody, RateBody
from hospitality.channels.web.router import require_guest_session
from hospitality.modules.feedback import api as feedback_api
from hospitality.modules.guests import api as guests_api
from hospitality.shared.errors import ErrorResponse
from hospitality.shared.tenancy import tenant_context

router = APIRouter(prefix="/g", tags=["web-chat"])

GuestAuth = Annotated[
    tuple[uuid.UUID, guests_api.ActiveGuestSession], Depends(require_guest_session)
]
_COMMON: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Строгий auth-only (ERR-WEB-002)"},
    404: {
        "model": ErrorResponse,
        "description": "Оценки выключены (ERR-FEEDBACK-001) или заявку нельзя оценить (002)",
    },
}


@router.get(widget.PATH.removeprefix(router.prefix), include_in_schema=False)
async def widget_script() -> Response:
    """Без тенанта и сессии, кэш навсегда — версию меняет адрес (`widget.URL`), как у обоев."""
    return Response(
        widget.SCRIPT,
        media_type="text/javascript; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{widget.VERSION}"',
        },
    )


@router.post(
    "/{tenant_slug}/{room_number}/feedback",
    summary="Палец вверх или вниз под итогом выполненной заявки (spec 0076)",
    responses={
        **_COMMON,
        409: {"model": ErrorResponse, "description": "Заявка уже оценена иначе (ERR-FEEDBACK-003)"},
    },
)
async def rate(body: RateBody, auth: GuestAuth) -> feedback_api.FeedbackRead:
    """Повтор той же оценки — прежний ответ без второй карточки (P-8). `passed_to_staff`
    у пальца вниз — карточка в службе эскалаций есть: только тогда гостю «передадим»."""
    tenant_id, session = auth
    with tenant_context(tenant_id):
        return await feedback_api.rate_request(
            body.request_id, stay_id=session.stay_id, rating=body.rating
        )


@router.post(
    "/{tenant_slug}/{room_number}/feedback/comment",
    summary="«Что было не так» после пальца вниз — один раз (spec 0076)",
    responses={
        **_COMMON,
        409: {
            "model": ErrorResponse,
            "description": "Не после пальца вниз или комментарий уже есть (ERR-FEEDBACK-004)",
        },
        422: {
            "model": ErrorResponse,
            "description": "Пустой или длиннее 500 знаков (ERR-PLATFORM-002)",
        },
    },
)
async def comment(body: CommentBody, auth: GuestAuth) -> feedback_api.FeedbackRead:
    """`passed_to_staff` — комментарий записан в карточку: только тогда гостю «передали»."""
    tenant_id, session = auth
    with tenant_context(tenant_id):
        return await feedback_api.add_comment(
            body.request_id, stay_id=session.stay_id, comment=body.comment
        )
