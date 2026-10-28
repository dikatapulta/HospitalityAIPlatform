"""Отметка «Оплачено» у заявки с оплатой при выполнении (spec 0038 Р-2, §4).

Отдельный файл, а не ещё одна функция в `service.py`: тот и без неё за
границей R-3 (~400 строк) — тот же довод, что у `day_summary.py`. Вход снаружи —
реэкспорт через `api.py` (контракт import-linter), как у всего в модуле.

Вторая половина правила — защита «Готово без отметки» (ERR-REQUESTS-006) —
живёт в `change_request_status`: переход проверяет домен, а не вызывающие.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from hospitality.modules.requests.events import RequestPaymentMarked
from hospitality.modules.requests.models import RequestStatus
from hospitality.modules.requests.schemas import ActingUser, ServiceRequestRead
from hospitality.modules.requests.service import (
    ERR_REQUESTS_PAYMENT_NOT_APPLICABLE,
    _ensure_own_service,
    _get_request_or_raise,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def mark_request_paid(
    request_id: uuid.UUID,
    *,
    acting_user: ActingUser,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> ServiceRequestRead:
    """Отметить, что деньги у сотрудника, и опубликовать `request.payment_marked`.

    Ставит только человек из кабинета (P-9): `acting_user` обязателен, его имя
    уходит в `paid_by_*`. Отметка применима к взятой (`in_progress`) заявке с
    признаком `payment_required` и без отметки; иначе — ERR-REQUESTS-008
    (409): заявка не взята, закрыта, без оплаты или уже оплачена. Повторное
    нажатие поэтому не перезаписывает, кто и когда взял деньги.

    `allowed_category_ids` — граница службы, как у `change_request_status`
    (spec 0045 §5, ревью PR #497 Н-4): чужая служба — ERR-REQUESTS-007 раньше
    проверки отметки, под той же блокировкой строки. Не финансовая запись: чек —
    в кассе отеля, денег и карт платформа не видит (NG-3). Строку журнала
    изменений добавит PR кнопки «Оплачено» (spec 0041 §2.2).
    """
    async with session_scope() as session:
        # FOR UPDATE: две одновременные отметки или отметка против отмены
        # решаются по актуальной строке, а не по прочитанной до чужого commit'а.
        request = await _get_request_or_raise(session, request_id, for_update=True)
        _ensure_own_service(request, allowed_category_ids)
        if (
            request.status is not RequestStatus.IN_PROGRESS
            or not request.payment_required
            or request.paid_at is not None
        ):
            logger.warning(
                "service_request_payment_mark_rejected",
                request_id=str(request.id),
                status=request.status.value,
                payment_required=request.payment_required,
                already_paid=request.paid_at is not None,
            )
            raise AppError(
                code=ERR_REQUESTS_PAYMENT_NOT_APPLICABLE,
                message=(
                    "Payment mark applies only to an in-progress request that requires "
                    "payment and is not marked paid yet"
                ),
                status_code=409,
            )
        request.paid_at = utc_now()
        request.paid_by_user_id = acting_user.user_id
        request.paid_by_display_name = acting_user.display_name
        await publish(session, RequestPaymentMarked(request_id=request.id))
    logger.info("service_request_payment_marked", request_id=str(request.id))
    return ServiceRequestRead.model_validate(request)
