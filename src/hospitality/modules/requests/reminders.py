"""Гость напоминает о своей заявке (spec 0070 §3, issue #834).

Модуль только считает: сколько раз гость напомнил и когда последний. Кого
звать, решает лестница кабинета (`staff_portal/ladder.py`, spec 0070 §5) — по
настройке отеля, кодом, а не моделью (P-5). Событий нет: сигнал кабинета
опрашивает заявки сам (spec 0039 §3).

Отдельный файл, а не функция в `service.py`: тот за границей R-3 — довод
`payment.py` и `sent.py`. Вход снаружи — реэкспорт через `api.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import select, update

from hospitality.modules.requests.models import ServiceRequest
from hospitality.modules.requests.schemas import ServiceRequestRead
from hospitality.modules.requests.service import _OPEN_STATUSES, _list_scoped
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def record_guest_reminder(request_id: uuid.UUID) -> ServiceRequestRead | None:
    """Засчитать напоминание гостя об открытой заявке; `None` — её нет или уже закрыли.

    Один атомарный UPDATE: два напоминания одновременно дают оба счёта, без
    чтения и блокировки. Время — `utc_now()` приложения, а не `now()` базы: с ним
    лестница сравнивает `created_at` и `claimed_at`, которые тоже ставит
    приложение (запись `docs/later/` ревью PR #838).
    """
    statement = (
        update(ServiceRequest)
        .where(ServiceRequest.id == request_id, ServiceRequest.status.in_(_OPEN_STATUSES))
        .values(
            guest_reminder_count=ServiceRequest.guest_reminder_count + 1,
            guest_reminded_at=utc_now(),
        )
        .returning(ServiceRequest)
        .execution_options(synchronize_session=False)
    )
    async with session_scope() as session:
        request = (await session.scalars(statement)).one_or_none()
        if request is None:
            return None
        read = ServiceRequestRead.model_validate(request)
    logger.info(
        "guest_reminder_recorded",
        request_id=str(read.id),
        reminder_count=read.guest_reminder_count,
    )
    return read


async def list_guest_reminded_requests(
    *, limit: int, category_ids: Collection[uuid.UUID] | None = None
) -> list[ServiceRequestRead]:
    """Открытые заявки, о которых гость напоминал; свежие напоминания сверху.

    Вопрос сигнала кабинета (spec 0070 §5): взятую заявку, о которой напомнил
    гость, лестница зовёт и без срока «долго в работе». Поднята ли она, решает
    вызывающая сторона по настройке отеля. `category_ids` — срез по службам
    (spec 0045 §5), `limit` — страховка от неограниченного скана.
    """
    query = select(ServiceRequest).where(
        ServiceRequest.status.in_(_OPEN_STATUSES),
        ServiceRequest.guest_reminded_at.is_not(None),
    )
    return await _list_scoped(
        query.order_by(ServiceRequest.guest_reminded_at.desc(), ServiceRequest.id).limit(limit),
        category_ids=category_ids,
    )
