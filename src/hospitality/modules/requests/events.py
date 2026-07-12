"""Доменные события модуля requests (Task 0012, P-6) — копия канона
`platform/events.py` (Task 0010).

События — публикуемый контракт модуля: уведомления служб (Task 0017),
аналитика и вебхуки подписываются на них, а не вызываются из `service.py`
напрямую. Подписчиков сам модуль не содержит; их регистрирует composition
root воркера (`hospitality/worker.py`).
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from hospitality.modules.requests.models import RequestStatus
from hospitality.shared.events import DomainEvent


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
