"""Заявки, которые службы отправили другим службам (spec 0063 §3, #700).

Отдельный файл, а не функция в `service.py`: тот за границей R-3 — довод
`payment.py` и `quota.py`. Вход снаружи — реэкспорт через `api.py` (контракт
import-linter).
"""

from __future__ import annotations

from sqlalchemy import select

from hospitality.modules.requests.models import RequestCategory, ServiceRequest
from hospitality.modules.requests.schemas import ServiceRequestRead
from hospitality.modules.requests.service import _list_scoped


async def list_requests_sent_by_services(
    service_keys: frozenset[str],
    *,
    limit: int,
    room_number: str | None = None,
    daily_number: int | None = None,
) -> list[ServiceRequestRead]:
    """Заявки, созданные формой сотрудником одной из `service_keys` и стоящие
    сейчас в службе не из `service_keys`; новые сверху, все статусы.

    Оба условия — в SQL до среза `limit` (канон очереди, #221): снимок служб
    создателя (`created_by_service_keys`) пересекается с набором, ключ нынешней
    категории в набор не входит. Переданная обратно в свою службу заявка
    отсюда уходит — она в очереди этой службы (одна заявка — одно место).
    Пустой набор — пустой ответ без запроса: без служб отправленного нет.
    Поиск по комнате и `#N` — как у ленты очереди (`_list_scoped`).
    """
    if not service_keys:
        return []
    keys = sorted(service_keys)
    query = (
        select(ServiceRequest)
        .join(RequestCategory, RequestCategory.id == ServiceRequest.category_id)
        .where(
            ServiceRequest.created_by_service_keys.overlap(keys),
            RequestCategory.key.not_in(keys),
        )
        .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id)
        .limit(limit)
    )
    return await _list_scoped(
        query, category_ids=None, room_number=room_number, daily_number=daily_number
    )
