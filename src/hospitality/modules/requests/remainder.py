"""«Готово» с остатком — остаток частично выполненной заявки (spec 0079 §2.1, issue #58).

Отдельный файл по доводу `payment.py`: `service.py` и без него за границей R-3.
Закрытие исходной и заявка на остаток — одна транзакция: остаток не теряется
между двумя записями, а отказ перехода (уже закрыта, чужая служба, без оплаты)
не оставляет остатка. Кто вправе и откуда зовут — каналы; домен знает только,
что остаток — новая заявка той же службы со ссылкой на исходную.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy.exc import IntegrityError

from hospitality.modules.requests.events import RequestInitiator
from hospitality.modules.requests.models import (
    RequestStatus,
    ServiceRequest,
    ServiceRequestOrigin,
)
from hospitality.modules.requests.schemas import (
    ActingUser,
    RemainderOutcome,
    ServiceRequestCreate,
    ServiceRequestRead,
)
from hospitality.modules.requests.service import (
    _DAILY_NUMBER_CONSTRAINT,
    _MAX_DAILY_NUMBER_ATTEMPTS,
    _apply_status_change,
    _insert_request,
    log_request_created,
    log_status_changed,
)
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def complete_with_remainder(
    request_id: uuid.UUID,
    *,
    resolution_note: str,
    initiator: RequestInitiator | None = None,
    acting_user: ActingUser | None = None,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> RemainderOutcome:
    """Закрыть заявку `done` с примечанием и завести остаток в очереди её службы.

    Переход — тот же, что `change_request_status(..., DONE, resolution_note=…)`:
    карта переходов, граница службы, оплата, `closed_*`, журнал и событие. Остаток —
    заявка `new` с `origin = remainder` и `parent_request_id`: служба, комната,
    Stay, язык гостя и суть — исходной, в `details` — «Остаток заявки #N от ДД.ММ.
    Примечание: …». Срочность, оплату, квоту и адресата остаток не наследует
    (spec 0079 §2.1). С `acting_user` — его строка журнала «создана», как у формы.

    Пустое примечание — `ValueError` (ошибка вызывающего кода): остатка без
    описания не бывает. Коллизия дневного номера — повтор всей транзакции.
    """
    note = resolution_note.strip()
    if not note:
        raise ValueError("remainder needs a non-empty resolution note")
    for attempt in range(_MAX_DAILY_NUMBER_ATTEMPTS):
        try:
            async with session_scope() as session:
                closed, old_status = await _apply_status_change(
                    session,
                    request_id,
                    RequestStatus.DONE,
                    resolution_note=note,
                    initiator=initiator,
                    acting_user=acting_user,
                    allowed_category_ids=allowed_category_ids,
                )
                remainder, category = await _insert_request(
                    session,
                    ServiceRequestCreate(
                        category_id=closed.category_id,
                        origin=ServiceRequestOrigin.REMAINDER,
                        summary=closed.summary,
                        details=remainder_details(closed, note),
                        room_number=closed.room_number,
                        guest_language=closed.guest_language,
                    ),
                    acting_user=acting_user,
                    stay_id=closed.stay_id,
                    parent_request_id=closed.id,
                )
        except IntegrityError as error:
            if (
                _DAILY_NUMBER_CONSTRAINT not in str(error)
                or attempt == _MAX_DAILY_NUMBER_ATTEMPTS - 1
            ):
                raise
            logger.info("daily_number_collision_retry", attempt=attempt + 1)
            continue
        log_status_changed(closed, old_status)
        log_request_created(remainder, category)
        logger.info(
            "service_request_remainder_created",
            request_id=str(closed.id),
            remainder_id=str(remainder.id),
            remainder_daily_number=remainder.daily_number,
        )
        return RemainderOutcome(
            closed=ServiceRequestRead.model_validate(closed),
            remainder=ServiceRequestRead.model_validate(remainder),
        )
    raise AssertionError("unreachable: цикл выше либо возвращает, либо пробрасывает")


def remainder_details(parent: ServiceRequest, note: str) -> str:
    """«Остаток заявки #12 от 10.11. Примечание: …» — строка карточки остатка.

    Номер и день — исходной: `#N` повторяется в разные дни, и без дня персонал
    не найдёт, к какой заявке остаток. Язык — персонала (ru), как у примечания.
    """
    label = "Остаток заявки"
    if parent.daily_number is not None:
        label += f" #{parent.daily_number}"
    if parent.service_day is not None:
        label += f" от {parent.service_day:%d.%m}"
    return f"{label}. Примечание: {note}"
