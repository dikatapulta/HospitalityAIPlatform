"""Возврат выполненной заявки в работу (spec 0080, issue #55).

Отдельный файл по доводу `payment.py`: `service.py` и без него за границей R-3.
«Готово», нажатое не на той заявке, исправляет сотрудник: `done → in_progress`
(`service.REOPEN_TRANSITION`) — сутки после закрытия, со строкой журнала и
событием `request.status_changed`. Поправку гостю шлёт подписчик события, а не
домен (P-6). Кто вправе и откуда зовут — каналы: кабинет и чат службы.

Круг «Готово ↔ Вернуть в работу» ограничен здесь (#1183): закрытие с остатком не
возвращается, а одну заявку возвращают не больше `MAX_REOPENS` раз. Без этого круг
над одной заявкой заводил невзятые остатки мимо предела ручного ввода (#971) и слал
гостю пары «выполнена» / «ещё не выполнена» без конца.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import select

from hospitality.modules.requests.events import RequestInitiator, RequestStatusChanged
from hospitality.modules.requests.models import (
    RequestCategory,
    RequestStatus,
    ServiceRequest,
    ServiceRequestOrigin,
)
from hospitality.modules.requests.schemas import ActingUser, ServiceRequestRead
from hospitality.modules.requests.service import (
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    ERR_REQUESTS_REOPEN_NOT_ALLOWED,
    REOPEN_TRANSITION,
    _ensure_own_service,
    _get_request_or_raise,
    _request_action,
    log_status_changed,
)
from hospitality.platform.staff_actions import StaffAction, record_staff_action
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Сколько после закрытия заявку можно вернуть (spec 0080 §2.2): промах замечают
# сразу или по жалобе гостя в тот же день; позже — уже новая просьба, новая заявка.
REOPEN_WINDOW: Final = timedelta(hours=24)

# Сколько раз одну заявку можно вернуть (spec 0080 §2.2, #1183): промах и второй промах
# на той же заявке — живой случай, третий — уже круг. Каждый возврат — поправка гостю,
# каждое следующее «Готово» с примечанием — вызов модели из дневного бюджета отеля.
MAX_REOPENS: Final = 2

# Запрос с витрины (`showroom_form`) не возвращается (spec 0080 §2.3) — вне #998.
# Заявки форм гостя (`guest_form` — заказ, бронь, АВР) возвращаются с #998: поправку
# и новое «Готово» шлют их подписчики (`channels/web/*/messages.py`), ответ брони
# снимает `table_booking.withdraw_answer_on_reopen`.
_NOT_REOPENABLE_ORIGINS: Final = frozenset({ServiceRequestOrigin.SHOWROOM_FORM})


def can_reopen(
    request: ServiceRequestRead, now: datetime | None = None, *, has_remainder: bool
) -> bool:
    """Можно ли вернуть заявку в работу сейчас — то же правило, что у `reopen_request`.

    Экран и кнопка чата спрашивают его, чтобы не рисовать «Вернуть в работу», которое
    домен отвергнет. `has_remainder` — завела ли заявка остаток
    (`list_remainder_parent_ids`): в строке заявки этого нет, а забыть нельзя — флаг
    обязательный. `now` по умолчанию — сейчас.
    """
    moment = now if now is not None else utc_now()
    return _reopenable(
        request.status,
        request.origin,
        request.closed_at,
        moment,
        reopen_count=request.reopen_count,
        has_remainder=has_remainder,
    )


async def list_remainder_parent_ids(request_ids: Collection[uuid.UUID]) -> frozenset[uuid.UUID]:
    """Какие из заявок завели остаток (spec 0079) — у них «Вернуть в работу» нет (#1183).

    Одним запросом на ленту, по индексу `parent_request_id`; пустой список — без запроса.
    """
    if not request_ids:
        return frozenset()
    async with session_scope() as session:
        found = await session.scalars(
            select(ServiceRequest.parent_request_id)
            .where(ServiceRequest.parent_request_id.in_(list(request_ids)))
            .distinct()
        )
        return frozenset(parent_id for parent_id in found if parent_id is not None)


async def reopen_allowed(request: ServiceRequestRead, now: datetime | None = None) -> bool:
    """`can_reopen` одной заявки с остатком из базы — ответ «Готово», карточка чата.

    Остаток спрашивается, только когда остальное правило уже «да»: у открытой или
    давней заявки запроса нет. Ленте — `list_remainder_parent_ids` одним запросом.
    """
    if not can_reopen(request, now, has_remainder=False):
        return False
    return request.id not in await list_remainder_parent_ids([request.id])


def _reopenable(
    status: RequestStatus,
    origin: ServiceRequestOrigin,
    closed_at: datetime | None,
    now: datetime,
    *,
    reopen_count: int,
    has_remainder: bool,
) -> bool:
    """Выполнена, не запрос с витрины, закрыта меньше суток назад, возвратов меньше
    `MAX_REOPENS` и остатка не заводила. `closed_at` None (закрыта до spec 0035) — срок
    не посчитать: нельзя. Остаток — работа уже в очереди отдельной заявкой: возврат
    оставил бы вторую на то же, а круг заводил бы новые остатки без предела (#1183)."""
    return (
        status is REOPEN_TRANSITION[0]
        and origin not in _NOT_REOPENABLE_ORIGINS
        and closed_at is not None
        and now - closed_at < REOPEN_WINDOW
        and reopen_count < MAX_REOPENS
        and not has_remainder
    )


async def reopen_request(
    request_id: uuid.UUID,
    *,
    initiator: RequestInitiator | None = None,
    acting_user: ActingUser | None = None,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> ServiceRequestRead:
    """Вернуть выполненную заявку в работу и опубликовать `request.status_changed`.

    `done → in_progress`: снимаются `closed_at`, `closed_by_*` и примечание к
    закрытию — заявка снова открыта, а следующее «Готово» напишет своё. Пара взятия
    остаётся: заявка возвращается тому, кто её делал («Вернуть в очередь» — дальше,
    если он ушёл). `reopen_count` растёт на 1 — по нему уведомитель гостя различает
    закрытия. Остаток, заведённый закрытием (spec 0079), живёт своей заявкой.

    Отказы, все под блокировкой строки: чужая служба — ERR-REQUESTS-007 (403), раньше
    прочих; заявка не `done` (вернули уже, отменена, открыта) — ERR-REQUESTS-003 (409);
    запрос с витрины, прошли сутки, возвратов уже `MAX_REOPENS` или закрытие завело
    остаток — ERR-REQUESTS-009 (409). Остаток ищется в той же транзакции: «Готово»
    с остатком пишет его вместе с `done` под той же блокировкой строки. С `acting_user` —
    строка журнала «Возвращена в работу» той же транзакцией; без неё (чат службы) — только лог.
    """
    from_status, to_status = REOPEN_TRANSITION
    async with session_scope() as session:
        request = await _get_request_or_raise(session, request_id, for_update=True)
        _ensure_own_service(request, allowed_category_ids)
        if request.status is not from_status:
            raise AppError(
                code=ERR_REQUESTS_INVALID_STATUS_TRANSITION,
                message=f"Cannot reopen a service request in status {request.status.value!r}",
                status_code=409,
            )
        has_remainder = (
            await session.scalar(
                select(ServiceRequest.id)
                .where(ServiceRequest.parent_request_id == request.id)
                .limit(1)
            )
            is not None
        )
        if not _reopenable(
            request.status,
            request.origin,
            request.closed_at,
            utc_now(),
            reopen_count=request.reopen_count,
            has_remainder=has_remainder,
        ):
            logger.warning(
                "service_request_reopen_rejected",
                request_id=str(request.id),
                origin=request.origin.value,
                reopen_count=request.reopen_count,
                has_remainder=has_remainder,
            )
            raise AppError(
                code=ERR_REQUESTS_REOPEN_NOT_ALLOWED,
                message="Service request can no longer be reopened",
                status_code=409,
            )
        request.status = to_status
        request.closed_at = None
        request.closed_by_user_id = None
        request.closed_by_display_name = None
        request.resolution_note = None
        request.reopen_count += 1
        if acting_user is not None:
            category = await session.get(RequestCategory, request.category_id)
            record = _request_action(request, category, StaffAction.REQUEST_REOPENED)
            record_staff_action(session, acting_user, record)
        await publish(
            session,
            RequestStatusChanged(
                request_id=request.id,
                old_status=from_status,
                new_status=to_status,
                initiator=initiator,
            ),
        )
    log_status_changed(request, from_status)
    logger.info(
        "service_request_reopened",
        request_id=str(request.id),
        reopen_count=request.reopen_count,
        with_actor=acting_user is not None,
    )
    return ServiceRequestRead.model_validate(request)
