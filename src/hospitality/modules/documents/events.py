"""Доменные события модуля documents (spec 0049 Р-4, P-6) — копия канона
`platform/events.py`.

`ActRequestSubmitted` публикуется в одной транзакции со строкой запроса;
подписчик — разбор реквизитов в воркере (`processing.handle_act_request_submitted`,
регистрирует `worker.py`). В payload только id: ни реквизитов, ни текста гостя.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from hospitality.shared.events import DomainEvent


class ActRequestSubmitted(DomainEvent):
    """Факт «гость прислал запрос реквизитов для АВР»."""

    event_name: ClassVar[str] = "act_request.submitted"

    act_request_id: uuid.UUID
