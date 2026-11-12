"""Возврат заявки формы в работу — общее у подписчиков форм веб-чата (#998, spec 0080 §4).

Заказ в номер, бронь столика и запрос АВР шлют гостю шаги своими подписчиками
(`menu/`, `table/`, `documents/messages.py`) с ключом на шаг. Выполненную заявку
формы теперь возвращают в работу (`requests.reopen_request`), и три правила общего
уведомителя итога (`channels/telegram/notifications.py`) нужны каждому из них:

- опоздавшее «выполнено» — заявку вернули раньше, чем воркер доставил шаг, — молчит;
- ключ шага закрытия получает номер возврата: следующее «выполнено» — новое сообщение;
- поправка гостю — только если «выполнено» отменённого закрытия ушло.
"""

from __future__ import annotations

from hospitality.channels.common.store import notification_already_sent
from hospitality.modules.requests import api as requests_api


def is_reopen(event: requests_api.RequestStatusChanged) -> bool:
    """Событие — возврат выполненной в работу (`done → in_progress`)."""
    return (
        event.old_status is requests_api.RequestStatus.DONE
        and event.new_status is requests_api.RequestStatus.IN_PROGRESS
    )


def numbered_key(base_key: str, reopen_count: int) -> str:
    """Ключ шага закрытия после `reopen_count` возвратов; у первого закрытия — прежний.

    Те же правила, что `notifications.guest_close_key`: уже отправленные шаги
    первого закрытия своих ключей не меняют.
    """
    return f"{base_key}:{reopen_count}" if reopen_count else base_key


def closing_is_late(
    request: requests_api.ServiceRequestRead, closed_to: requests_api.RequestStatus
) -> bool:
    """Шаг закрытия опоздал: заявку уже вернули в работу, «выполнено» было бы неправдой.

    Без возврата статус от закрытия не уходит — `done` и `cancelled` терминальны.
    """
    return bool(request.reopen_count) and request.status is not closed_to


async def closing_was_told(request: requests_api.ServiceRequestRead, base_key: str) -> bool:
    """Гостю ушло «выполнено» закрытия, которое этот возврат отменил."""
    if request.reopen_count < 1:  # событие возврата пишет только reopen_request
        return False
    return await notification_already_sent(numbered_key(base_key, request.reopen_count - 1))


def reopen_key(base_key: str, request: requests_api.ServiceRequestRead) -> str:
    """Ключ поправки: по номеру возврата — второй возврат даёт новую поправку."""
    return f"{base_key}:{request.reopen_count}"
