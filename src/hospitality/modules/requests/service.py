"""Бизнес-логика модуля requests (Task 0012, FOUNDATION §5.2).

Единственный путь создать заявку или сменить её статус. Каждая функция —
одна транзакция по канону P-4/P-12: вызывается внутри `tenant_context`,
открывает `session_scope()`; события публикуются в той же транзакции,
что бизнес-запись (P-6). Ожидаемые ошибки — `AppError` с кодами каталога
(`docs/runbooks/errors.md`, R-8).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, tzinfo
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.requests.events import RequestCreated, RequestStatusChanged
from hospitality.modules.requests.models import RequestCategory, RequestStatus, ServiceRequest
from hospitality.modules.requests.schemas import (
    RequestCategoryCreate,
    RequestCategoryRead,
    ServiceRequestCreate,
    ServiceRequestPage,
    ServiceRequestRead,
)
from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_REQUESTS_CATEGORY_NOT_FOUND = "ERR-REQUESTS-001"
ERR_REQUESTS_REQUEST_NOT_FOUND = "ERR-REQUESTS-002"
ERR_REQUESTS_INVALID_STATUS_TRANSITION = "ERR-REQUESTS-003"
ERR_REQUESTS_CATEGORY_KEY_TAKEN = "ERR-REQUESTS-004"

# Имя уникального индекса дневного номера (модель) — оно же опознаётся в тексте
# IntegrityError при гонке присвоения. Число попыток пересчёта номера с запасом:
# коллизия возможна лишь между одновременными создателями одного тенанта в один
# день (десятки в сутки) — практически 1 повтор, 5 хватает на любой всплеск.
_DAILY_NUMBER_CONSTRAINT: Final = "uq_service_requests_daily_number"
_MAX_DAILY_NUMBER_ATTEMPTS: Final = 5

# Жизненный цикл заявки (§5.2, ADR-013): new → in_progress → done/cancelled.
# Отменить можно любую незавершённую заявку; done и cancelled — терминальные.
STATUS_TRANSITIONS: Final[dict[RequestStatus, frozenset[RequestStatus]]] = {
    RequestStatus.NEW: frozenset({RequestStatus.IN_PROGRESS, RequestStatus.CANCELLED}),
    RequestStatus.IN_PROGRESS: frozenset({RequestStatus.DONE, RequestStatus.CANCELLED}),
    RequestStatus.DONE: frozenset(),
    RequestStatus.CANCELLED: frozenset(),
}

# Незакрытые статусы — те, из которых ещё есть переход (не done/cancelled).
# Выводится из карты переходов, чтобы не разъехаться с ней при правках.
_OPEN_STATUSES: Final = frozenset(
    status for status, transitions in STATUS_TRANSITIONS.items() if transitions
)


async def create_category(data: RequestCategoryCreate) -> RequestCategoryRead:
    """Создать категорию заявок у текущего тенанта.

    В Phase 0 категории заводят сиды и тесты; с кабинетом персонала (Phase 1)
    сюда придёт HTTP API. Дубликат `key` у тенанта — ERR-REQUESTS-004.
    """
    category = RequestCategory(key=data.key, name=data.name)
    try:
        async with session_scope() as session:
            session.add(category)
            await session.flush()
    except IntegrityError as error:
        # Только нарушение уникальности (tenant_id, key) — «ключ занят»;
        # прочие IntegrityError (например, FK) — не ожидаемая бизнес-ошибка.
        if "uq_request_categories_tenant_id" not in str(error):
            raise
        raise AppError(
            code=ERR_REQUESTS_CATEGORY_KEY_TAKEN,
            message=f"Request category with key {data.key!r} already exists",
            status_code=409,
        ) from None
    logger.info("request_category_created", category_id=str(category.id), category_key=data.key)
    return RequestCategoryRead.model_validate(category)


async def list_categories() -> list[RequestCategoryRead]:
    """Категории текущего тенанта, по `key` — стабильный порядок для API и UI."""
    async with session_scope() as session:
        categories = await session.scalars(select(RequestCategory).order_by(RequestCategory.key))
        return [RequestCategoryRead.model_validate(category) for category in categories]


async def create_request(data: ServiceRequestCreate) -> ServiceRequestRead:
    """Создать заявку в статусе `new` и опубликовать `request.created`.

    Категория ищется тенантной сессией: чужая или несуществующая категория
    неразличимы (RLS, P-4) — обе дают ERR-REQUESTS-001.

    Заявке присваивается дневной номер `#N`, уникальный в паре
    `(тенант, день отеля)` (issue #38, заход 2а). Защита от гонки — сам
    уникальный индекс: если параллельный создатель занял тот же номер, INSERT
    падает с IntegrityError, номер пересчитывается и попытка повторяется
    (номер не «дырявится» и не дублируется).
    """
    for attempt in range(_MAX_DAILY_NUMBER_ATTEMPTS):
        try:
            async with session_scope() as session:
                category = await session.get(RequestCategory, data.category_id)
                if category is None:
                    raise AppError(
                        code=ERR_REQUESTS_CATEGORY_NOT_FOUND,
                        message=f"Request category {data.category_id} does not exist",
                        status_code=404,
                    )
                service_day = await _hotel_service_day(session)
                request = ServiceRequest(
                    category_id=category.id,
                    summary=data.summary,
                    details=data.details,
                    room_number=data.room_number,
                    guest_language=data.guest_language,
                    service_day=service_day,
                    daily_number=await _next_daily_number(session, service_day),
                )
                session.add(request)
                await session.flush()
                await publish(
                    session,
                    RequestCreated(
                        request_id=request.id, category_id=category.id, summary=data.summary
                    ),
                )
            logger.info(
                "service_request_created",
                request_id=str(request.id),
                category_key=category.key,
                daily_number=request.daily_number,
            )
            return ServiceRequestRead.model_validate(request)
        except IntegrityError as error:
            # Только коллизия дневного номера — ожидаемая гонка, повторяем.
            # Последняя попытка или иной IntegrityError (FK и т.п.) — пробрасываем.
            if (
                _DAILY_NUMBER_CONSTRAINT not in str(error)
                or attempt == _MAX_DAILY_NUMBER_ATTEMPTS - 1
            ):
                raise
            logger.info("daily_number_collision_retry", attempt=attempt + 1)
    raise AssertionError("unreachable: цикл выше либо возвращает, либо пробрасывает")


async def _hotel_service_day(session: AsyncSession, /) -> date:
    """Календарный день отеля «сейчас» — база сброса дневного номера (§9).

    День берётся по tz из конфига тенанта (в БД — UTC, локальная дата — слой
    представления). Тенант без конфига (онбординг не завершён; служебный
    smoke-тенант) — деградация на UTC, а не отказ: заявку гостя нумеруем всегда.
    """
    zone: tzinfo = UTC
    try:
        config = await load_tenant_config(session, current_tenant_id())
        zone = config.tzinfo
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        logger.warning("daily_number_service_day_utc_fallback", error_code=error.code)
    return utc_now().astimezone(zone).date()


async def _next_daily_number(session: AsyncSession, service_day: date, /) -> int:
    """Следующий свободный дневной номер: `max(daily_number) + 1` за этот день.

    RLS ограничивает выборку текущим тенантом (P-4), поэтому номер уникален в
    паре `(тенант, день)`. Первое чтение при пустом дне даёт `#1`.
    """
    current_max = await session.scalar(
        select(func.max(ServiceRequest.daily_number)).where(
            ServiceRequest.service_day == service_day
        )
    )
    return (current_max or 0) + 1


async def find_open_requests_by_daily_number(daily_number: int) -> list[ServiceRequestRead]:
    """Незакрытые заявки тенанта с этим дневным номером (резолв команды /done N).

    Номер сбрасывается за сутки: в редкой ситуации незакрытая заявка прошлого
    дня делит `#N` с сегодняшней — тогда список содержит несколько кандидатов, и
    вызывающая сторона (staff.py) просит уточнить. Терминальные (done/cancelled)
    не возвращаются: закрытую заявку номером не трогают. Новые сверху.
    """
    async with session_scope() as session:
        rows = await session.scalars(
            select(ServiceRequest)
            .where(
                ServiceRequest.daily_number == daily_number,
                ServiceRequest.status.in_(_OPEN_STATUSES),
            )
            .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id)
        )
        return [ServiceRequestRead.model_validate(row) for row in rows]


async def list_requests(*, limit: int, offset: int) -> ServiceRequestPage:
    """Страница заявок текущего тенанта, новые сверху (канон пагинации Task 0013).

    Границы limit/offset валидирует HTTP-слой (Query в router.py); сервис
    доверяет вызывающей стороне внутри процесса. Сортировка стабильна:
    `created_at DESC` с tie-break по `id` — страницы не перекрываются
    при равных временах создания.
    """
    async with session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(ServiceRequest))
        rows = await session.scalars(
            select(ServiceRequest)
            .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id)
            .limit(limit)
            .offset(offset)
        )
        items = [ServiceRequestRead.model_validate(row) for row in rows]
    return ServiceRequestPage(items=items, total=total or 0, limit=limit, offset=offset)


async def change_request_status(
    request_id: uuid.UUID, new_status: RequestStatus
) -> ServiceRequestRead:
    """Перевести заявку в новый статус и опубликовать `request.status_changed`.

    Допустимые переходы — `STATUS_TRANSITIONS`; недопустимый (в том числе
    из терминального статуса или в тот же самый) — ERR-REQUESTS-003.
    """
    async with session_scope() as session:
        # FOR UPDATE: конкурентная смена статуса той же заявки валидируется
        # по актуальному значению, а не по прочитанному до чужого commit'а.
        request = await _get_request_or_raise(session, request_id, for_update=True)
        old_status = request.status
        if new_status not in STATUS_TRANSITIONS[old_status]:
            raise AppError(
                code=ERR_REQUESTS_INVALID_STATUS_TRANSITION,
                message=(
                    f"Cannot change request status from {old_status.value!r} "
                    f"to {new_status.value!r}"
                ),
                status_code=409,
            )
        request.status = new_status
        await publish(
            session,
            RequestStatusChanged(
                request_id=request.id, old_status=old_status, new_status=new_status
            ),
        )
    logger.info(
        "service_request_status_changed",
        request_id=str(request.id),
        old_status=old_status.value,
        new_status=new_status.value,
    )
    return ServiceRequestRead.model_validate(request)


async def get_request(request_id: uuid.UUID) -> ServiceRequestRead:
    """Прочитать заявку текущего тенанта; чужая или несуществующая — ERR-REQUESTS-002."""
    async with session_scope() as session:
        request = await _get_request_or_raise(session, request_id)
    return ServiceRequestRead.model_validate(request)


async def _get_request_or_raise(
    session: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> ServiceRequest:
    query = select(ServiceRequest).where(ServiceRequest.id == request_id)
    if for_update:
        query = query.with_for_update()
    request = (await session.execute(query)).scalar_one_or_none()
    if request is None:
        raise AppError(
            code=ERR_REQUESTS_REQUEST_NOT_FOUND,
            message=f"Service request {request_id} does not exist",
            status_code=404,
        )
    return request
