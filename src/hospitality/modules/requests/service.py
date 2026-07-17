"""Бизнес-логика модуля requests (Task 0012, FOUNDATION §5.2).

Единственный путь создать заявку или сменить её статус. Каждая функция —
одна транзакция по канону P-4/P-12: вызывается внутри `tenant_context`,
открывает `session_scope()`; события публикуются в той же транзакции,
что бизнес-запись (P-6). Ожидаемые ошибки — `AppError` с кодами каталога
(`docs/runbooks/errors.md`, R-8).
"""

from __future__ import annotations

import uuid
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
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_REQUESTS_CATEGORY_NOT_FOUND = "ERR-REQUESTS-001"
ERR_REQUESTS_REQUEST_NOT_FOUND = "ERR-REQUESTS-002"
ERR_REQUESTS_INVALID_STATUS_TRANSITION = "ERR-REQUESTS-003"
ERR_REQUESTS_CATEGORY_KEY_TAKEN = "ERR-REQUESTS-004"

# Жизненный цикл заявки (§5.2): new → assigned → in_progress → done/cancelled.
# Отменить можно любую незавершённую заявку; done и cancelled — терминальные.
STATUS_TRANSITIONS: Final[dict[RequestStatus, frozenset[RequestStatus]]] = {
    RequestStatus.NEW: frozenset({RequestStatus.ASSIGNED, RequestStatus.CANCELLED}),
    RequestStatus.ASSIGNED: frozenset({RequestStatus.IN_PROGRESS, RequestStatus.CANCELLED}),
    RequestStatus.IN_PROGRESS: frozenset({RequestStatus.DONE, RequestStatus.CANCELLED}),
    RequestStatus.DONE: frozenset(),
    RequestStatus.CANCELLED: frozenset(),
}


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
    """
    async with session_scope() as session:
        category = await session.get(RequestCategory, data.category_id)
        if category is None:
            raise AppError(
                code=ERR_REQUESTS_CATEGORY_NOT_FOUND,
                message=f"Request category {data.category_id} does not exist",
                status_code=404,
            )
        request = ServiceRequest(
            category_id=category.id,
            summary=data.summary,
            details=data.details,
            room_number=data.room_number,
        )
        session.add(request)
        await session.flush()
        await publish(
            session,
            RequestCreated(request_id=request.id, category_id=category.id, summary=data.summary),
        )
    logger.info(
        "service_request_created",
        request_id=str(request.id),
        category_key=category.key,
    )
    return ServiceRequestRead.model_validate(request)


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
