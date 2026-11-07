"""Дописать строку в подробности невзятой заявки (spec 0039 §2.5, #863).

Опора дедупликации эскалаций: пока у диалога есть невзятая эскалационная
карточка, новая просьба гостя не заводит вторую, а дописывается в первую — иначе
гостю сказано «передал», а в кабинете её нет. Отдельный файл, а не ещё одна
функция в `service.py`: тот и без неё за границей R-3 (~400 строк) — тот же довод,
что у `payment.py`. Вход снаружи — реэкспорт через `api.py`.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hospitality.modules.requests.models import RequestStatus, ServiceRequest
from hospitality.modules.requests.schemas import REQUEST_DETAILS_MAX_CHARS, ServiceRequestRead
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def append_request_details(request_id: uuid.UUID, line: str) -> ServiceRequestRead | None:
    """Дописать `line` новой строкой в `details` заявки в статусе `new`.

    None — дописать некуда, и вызывающий заводит новую заявку: заявки у тенанта
    нет, её уже взяли или закрыли (взявший мог уйти, не перечитав карточку), или
    строка не влезает в предел подробностей схемы. Ничего не обрезается: обрезка
    съела бы ровно ту просьбу, ради которой вызов.

    Блокировка строки (FOR UPDATE): две одновременные дописки не теряют друг
    друга, а «Взять» между проверкой статуса и записью невозможно. Событие не
    публикуется и журнал изменений не пишется: это не действие человека и не
    новая заявка — карточка и так в очереди.
    """
    async with session_scope() as session:
        request = (
            await session.execute(
                select(ServiceRequest).where(ServiceRequest.id == request_id).with_for_update()
            )
        ).scalar_one_or_none()
        if request is None or request.status is not RequestStatus.NEW:
            return None
        details = f"{request.details}\n{line}" if request.details else line
        if len(details) > REQUEST_DETAILS_MAX_CHARS:
            logger.info("service_request_details_full", request_id=str(request.id))
            return None
        request.details = details
    logger.info("service_request_details_appended", request_id=str(request.id))
    return ServiceRequestRead.model_validate(request)
