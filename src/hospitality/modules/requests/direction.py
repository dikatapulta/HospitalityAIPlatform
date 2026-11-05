"""«Направить» — старший называет, кто возьмёт невзятую заявку (spec 0053 §4а, #856).

Отдельный файл по доводу `payment.py`: `service.py` и без него за границей R-3.
Модуль не знает ни этажей, ни служб уборки: он помнит, кому направили, и отдаёт
это лентам (`FloorScope` пропускает направленную её адресату). Кто вправе
направлять и кому можно — решает кабинет (`staff_portal/floors.py`), как и
членство адресата в службе: членство — платформенная таблица.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from hospitality.modules.requests.models import RequestCategory, RequestStatus
from hospitality.modules.requests.schemas import ActingUser, ServiceRequestRead
from hospitality.modules.requests.service import (
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    _ensure_own_service,
    _get_request_or_raise,
    _request_action,
)
from hospitality.platform.staff_actions import StaffAction, record_staff_action
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def direct_request(
    request_id: uuid.UUID,
    *,
    to_user_id: uuid.UUID,
    to_display_name: str,
    acting_user: ActingUser,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> ServiceRequestRead:
    """Направить невзятую заявку сотруднику: `directed_to_*` и строка журнала.

    Только `new`: взятую делает уже тот, кто взял, закрытую — никто; иначе
    ERR-REQUESTS-003 (409) — то же «уже взята или закрыта», что у двух «Взять»
    разом. Повторное направление перезаписывает адресата — старший передумал.
    Статус не меняется и события нет: заявку по-прежнему никто не взял, а
    звучит она адресату через сигнал кабинета, который читает ленту.
    `allowed_category_ids` — граница службы, как у `change_request_status`
    (ERR-REQUESTS-007 под той же блокировкой строки). Имени адресата в журнале
    нет (spec 0041 §2.1): строка говорит «направлена», а кому — карточка.
    """
    async with session_scope() as session:
        request = await _get_request_or_raise(session, request_id, for_update=True)
        _ensure_own_service(request, allowed_category_ids)
        if request.status is not RequestStatus.NEW:
            raise AppError(
                code=ERR_REQUESTS_INVALID_STATUS_TRANSITION,
                message=f"Cannot direct request in status {request.status.value!r}",
                status_code=409,
            )
        request.directed_to_user_id = to_user_id
        request.directed_to_display_name = to_display_name
        category = await session.get(RequestCategory, request.category_id)
        record_staff_action(
            session, acting_user, _request_action(request, category, StaffAction.REQUEST_DIRECTED)
        )
    logger.info(
        "service_request_directed",
        request_id=str(request.id),
        to_user_id=str(to_user_id),
        user_id=str(acting_user.user_id),
    )
    return ServiceRequestRead.model_validate(request)
