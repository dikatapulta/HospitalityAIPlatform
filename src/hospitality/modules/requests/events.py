"""Доменные события модуля requests (Task 0012, P-6) — копия канона
`platform/events.py` (Task 0010).

События — публикуемый контракт модуля: уведомления служб (Task 0017),
аналитика и вебхуки подписываются на них, а не вызываются из `service.py`
напрямую. Подписчиков сам модуль не содержит; их регистрирует composition
root воркера (`hospitality/worker.py`).
"""

from __future__ import annotations

import enum
import uuid
from typing import ClassVar

from hospitality.modules.requests.models import RequestStatus
from hospitality.shared.events import DomainEvent


class RequestInitiator(enum.StrEnum):
    """Кто инициировал переход статуса (spec 0025): факт о ДЕЙСТВИИ, из состояния
    БД не выводится, поэтому едет в событии. По нему подписчики выбирают адресата
    уведомления: гостю не сообщают о его собственной отмене, а персоналу — сообщают.
    """

    GUEST = "guest"
    STAFF = "staff"


class RequestCreated(DomainEvent):
    """Факт «заявка создана» — публикуется в одной транзакции с самой заявкой."""

    event_name: ClassVar[str] = "request.created"

    request_id: uuid.UUID
    category_id: uuid.UUID
    summary: str


class RequestStatusChanged(DomainEvent):
    """Факт «статус заявки изменён» — по одному событию на каждый переход."""

    event_name: ClassVar[str] = "request.status_changed"

    request_id: uuid.UUID
    old_status: RequestStatus
    new_status: RequestStatus
    # Аддитивное поле (§13.5, Уровень B; spec 0025): None — инициатор не указан
    # (старые строки outbox и пути, не передающие его: staff.py, HTTP-роутер) —
    # для подписчиков это прежнее поведение.
    initiator: RequestInitiator | None = None
