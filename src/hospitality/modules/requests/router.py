"""CANONICAL ENDPOINT: HTTP API заявок (Task 0013, FOUNDATION §11, §13.5, P-7).

Эталон REST-роутера платформы — новый роутер копирует этот файл:

- путь начинается с `/api/v1/` (§13.5: версия публичного API с первого дня);
- каждый эндпоинт рождается аутентифицированным (§11): зависимость
  `require_authenticated_tenant` на уровне роутера; тенанта устанавливает
  `TenantContextMiddleware` по сервисному токену — эндпоинт тенанта не
  выбирает и `tenant_id` в схемах не принимает;
- границы — Pydantic-схемы модуля (P-7); ошибки — канонический конверт
  `ErrorResponse` с кодами каталога (R-8), задокументированы в `responses`,
  чтобы OpenAPI отражал реальный контракт;
- пагинация списков — `limit`/`offset` + `total` (`ServiceRequestPage`);
- роутер тонкий: только HTTP-переходник к `service.py`, бизнес-логики нет.

Снаружи модуля роутер доступен через реэкспорт в `api.py`; подключает его
только composition root (`hospitality/app.py`).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Security

from hospitality.modules.requests.schemas import (
    RequestCategoryRead,
    ServiceRequestCreate,
    ServiceRequestPage,
    ServiceRequestRead,
    ServiceRequestStatusUpdate,
)
from hospitality.modules.requests.service import (
    change_request_status,
    create_request,
    get_request,
    list_categories,
    list_requests,
)
from hospitality.platform.auth import require_authenticated_tenant
from hospitality.shared.errors import ErrorResponse

router = APIRouter(
    prefix="/api/v1/requests",
    tags=["requests"],
    dependencies=[Security(require_authenticated_tenant)],
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Нет или неверный сервисный токен (ERR-PLATFORM-007)",
        }
    },
)


@router.post(
    "",
    status_code=201,
    summary="Создать заявку",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Категория не найдена у тенанта (ERR-REQUESTS-001)",
        }
    },
)
async def create_service_request(data: ServiceRequestCreate) -> ServiceRequestRead:
    return await create_request(data)


@router.get("", summary="Список заявок тенанта, новые сверху")
async def list_service_requests(
    limit: Annotated[int, Query(ge=1, le=100, description="Размер страницы")] = 50,
    offset: Annotated[int, Query(ge=0, description="Сдвиг от начала списка")] = 0,
) -> ServiceRequestPage:
    return await list_requests(limit=limit, offset=offset)


# Объявлен раньше "/{request_id}": иначе слово "categories" матчится как UUID
# заявки и уходит в 422 вместо списка категорий.
@router.get("/categories", summary="Категории заявок тенанта")
async def list_request_categories() -> list[RequestCategoryRead]:
    return await list_categories()


@router.get(
    "/{request_id}",
    summary="Заявка по id",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Заявка не найдена у тенанта (ERR-REQUESTS-002)",
        }
    },
)
async def get_service_request(request_id: uuid.UUID) -> ServiceRequestRead:
    return await get_request(request_id)


@router.post(
    "/{request_id}/status",
    summary="Перевести заявку по жизненному циклу",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Заявка не найдена у тенанта (ERR-REQUESTS-002)",
        },
        409: {
            "model": ErrorResponse,
            "description": "Недопустимый переход статуса (ERR-REQUESTS-003)",
        },
    },
)
async def change_service_request_status(
    request_id: uuid.UUID, data: ServiceRequestStatusUpdate
) -> ServiceRequestRead:
    return await change_request_status(
        request_id, data.status, resolution_note=data.resolution_note
    )
