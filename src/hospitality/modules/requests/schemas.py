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

from hospitality.modules.requests.models import RequestStatus, ServiceRequestOrigin


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
    # Источник заявки (spec 0035 §4) — обязателен и БЕЗ умолчания: каждый
    # создатель называет его явно. Умолчание здесь опаснее обычного — путь,
    # забывший его выставить, молча подмешался бы в одну из двух долей, из
    # которых считается Exit-критерий Phase 1 («тихая ложь»). AI-инструмент
    # передаёт `guest_chat`, форма кабинета — `staff_manual`, HTTP-эндпоинт
    # публичной двери — то, что прислали.
    origin: ServiceRequestOrigin
    summary: str = Field(min_length=1, max_length=500)
    details: str | None = Field(default=None, max_length=4000)
    room_number: str | None = Field(default=None, max_length=20)
    # Язык гостя (ISO 639-1, «kk»/«ru»/…) для статусных уведомлений (spec 0021
    # П-1). Схема строгая (граница API, R-6); терпимая нормализация сырого
    # значения от модели — забота AI-инструмента, не домена.
    guest_language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    # Срочная заявка (GLOSSARY, spec 0034 §5): промедление грозит здоровью,
    # безопасности или имуществу. Домен на признак не смотрит — он его хранит и
    # отдаёт: гейт подтверждения снимает AI-слой (ADR-018), ночную доставку
    # ветвит канал (issue #212). Умолчание False — заявка обычная.
    is_urgent: bool = False


class ActingUser(BaseModel):
    """Кто выполняет действие персонала из кабинета (spec 0033 §5, PR D).

    Снапшот на момент действия: id платформенного User + display_name — модуль
    не ходит в платформенные таблицы, имя передаёт вызывающая сторона
    (`staff_portal` берёт его из `StaffContext`). Telegram-путь персонала
    идентичности не несёт (ADR-008 §7) и передаёт None вместо этой схемы.
    """

    user_id: uuid.UUID
    display_name: str = Field(min_length=1, max_length=255)


class ServiceRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category_id: uuid.UUID
    status: RequestStatus
    summary: str
    details: str | None
    room_number: str | None
    # Дневной номер `#N` для глаз/речи/отчёта (issue #38, заход 2а); None у
    # доскелетных заявок, созданных до миграции 0010.
    daily_number: int | None
    guest_language: str | None
    # Срочная заявка (spec 0034 §5): маркер 🚨 в уведомлении службе и в очереди
    # кабинета; ночную доставку по нему ветвит issue #212.
    is_urgent: bool
    # Примечание персонала к закрытию (частичное выполнение / причина отмены),
    # по-русски; см. ServiceRequestStatusUpdate (spec 0021 П-4).
    resolution_note: str | None
    # Кто взял заявку в работу (spec 0033 §5): id User и снапшот имени на момент
    # взятия. Оба None — заявку никто не брал из кабинета (в т.ч. Telegram-путь).
    claimed_by_user_id: uuid.UUID | None
    claimed_by_display_name: str | None
    # Источник и метки времени (spec 0035 §3–§4): чем заявка была и когда её
    # взяли/закрыли. `claimed_at`/`closed_at` пишутся из любого канала, поэтому
    # None у `claimed_at` — «ещё не брали», а не «брали не из кабинета».
    # Имени ЗАКРЫВШЕГО здесь нет намеренно (§13): из пары «кто закрыл» наружу
    # отдаётся только момент. Имя взявшего — отдаётся (`claimed_by_display_name`
    # выше, spec 0033 §5), так что общего правила «имён в схеме нет» тут нет.
    origin: ServiceRequestOrigin
    claimed_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ServiceRequestStatusUpdate(BaseModel):
    """Тело смены статуса (Task 0013): целевой статус + примечание закрытия.

    Допустимость перехода проверяет `change_request_status` по
    `STATUS_TRANSITIONS`; неизвестное значение статуса отсекается валидацией.
    `resolution_note` — аддитивное поле (§13.5, spec 0021 П-4): осмысленно
    только на терминальном переходе, на прочих сервис его игнорирует.
    """

    status: RequestStatus
    resolution_note: str | None = Field(default=None, max_length=500)


class ServiceRequestPage(BaseModel):
    """Канон страницы списка API (Task 0013): items + total и параметры среза.

    `total` — общее число строк тенанта по фильтру (для пагинатора в UI);
    limit/offset возвращаются эхом, чтобы ответ был самодостаточным.
    """

    items: list[ServiceRequestRead]
    total: int
    limit: int
    offset: int
