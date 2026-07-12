"""Pydantic-схемы границ модуля requests (Task 0012, R-6, P-7).

Сервисные функции принимают `*Create` и возвращают `*Read` — ORM-объекты
наружу модуля не выходят. Эти же схемы переиспользуют HTTP API (Task 0013)
и AI-инструменты (Task 0015). `tenant_id` в схемах отсутствует намеренно:
тенанта задаёт контекст (P-4), вызывающая сторона его не выбирает.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.requests.models import RequestStatus


class RequestCategoryCreate(BaseModel):
    # Формат ключа — как slug тенанта: латиница/цифры/дефисы, стабильный
    # идентификатор для конфигов, AI-инструментов и сидов.
    key: str = Field(min_length=1, max_length=63, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    name: str = Field(min_length=1, max_length=255)


class RequestCategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    name: str
    created_at: datetime
    updated_at: datetime


class ServiceRequestCreate(BaseModel):
    category_id: uuid.UUID
    summary: str = Field(min_length=1, max_length=500)
    details: str | None = Field(default=None, max_length=4000)
    room_number: str | None = Field(default=None, max_length=20)


class ServiceRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_id: uuid.UUID
    status: RequestStatus
    summary: str
    details: str | None
    room_number: str | None
    created_at: datetime
    updated_at: datetime


class ServiceRequestStatusUpdate(BaseModel):
    """Тело смены статуса (Task 0013): только целевой статус.

    Допустимость перехода проверяет `change_request_status` по
    `STATUS_TRANSITIONS`; неизвестное значение статуса отсекается валидацией.
    """

    status: RequestStatus


class ServiceRequestPage(BaseModel):
    """Канон страницы списка API (Task 0013): items + total и параметры среза.

    `total` — общее число строк тенанта по фильтру (для пагинатора в UI);
    limit/offset возвращаются эхом, чтобы ответ был самодостаточным.
    """

    items: list[ServiceRequestRead]
    total: int
    limit: int
    offset: int
