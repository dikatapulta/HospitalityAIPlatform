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


class RequestCategoryChanged(DomainEvent):
    """Факт «заявку передали в другую службу» (spec 0042 §2.2).

    Если заявка была взята, та же транзакция публикует и `request.status_changed`
    (`in_progress → new`): передача возвращает её в очередь новой службы. Подписчика
    пока нет — у пилотного отеля заявки только в кабинете; оповещение чата новой
    службы в Telegram ждёт первого отеля с ботом (spec 0042 §2.5).
    """

    event_name: ClassVar[str] = "request.category_changed"

    request_id: uuid.UUID
    old_category_id: uuid.UUID
    new_category_id: uuid.UUID


class RequestPaymentMarked(DomainEvent):
    """Факт «сотрудник отметил оплату» (spec 0038 Р-2, `mark_request_paid`).

    Отметка — не переход статуса, поэтому у неё своё событие: подписчик заказа
    шлёт по нему гостю «Оплата получена» (spec 0038 §5). Кто и когда — в заявке
    (`paid_by_*`, `paid_at`); событие несёт только ключ.
    """

    event_name: ClassVar[str] = "request.payment_marked"

    request_id: uuid.UUID
