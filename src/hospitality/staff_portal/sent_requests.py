"""Вкладка «Отправленные» страницы «Заявки» (spec 0063 §3–§5, #700).

Заявки, которые сотрудники служб смотрящего создали формой в другие службы.
Только просмотр: карточка без действий, право действовать вкладка не меняет —
действие над заявкой чужой службы домен по-прежнему отвергает (403
`ERR-REQUESTS-007`, spec 0045 §5). Отдельный файл — `queue.py` за пределом R-3;
лента, поиск и разметка — общие с очередью.

Фильтры очереди (служба, «мои») вкладка не применяет: ссылки вкладок их несут,
а срез по своим службам дал бы пустую ленту — свои службы вкладка исключает
по определению.
"""

from __future__ import annotations

from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal.queue_filters import QueueSearch

TAB_LABEL: Final = "Отправленные"
TAB_HINT: Final = "Заявки, которые ваша служба отправила другим службам"

# Все кнопки карточки очереди (`queue._card`); во вкладке они погашены.
_ACTION_FLAGS: Final = (
    "can_claim",
    "can_complete",
    "can_requeue",
    "can_transfer",
    "can_cancel",
    "can_direct",
)


def has_tab(staff: StaffContext) -> bool:
    """Вкладка — у сотрудника со службами. Менеджеру службы не положены
    (весь отель он видит в очереди и журнале), у сотрудника без служб очередь
    говорит, что делать."""
    return bool(staff.service_keys)


async def list_sent(
    staff: StaffContext, search: QueueSearch, *, limit: int
) -> list[requests_api.ServiceRequestRead]:
    """Лента вкладки: новые сверху, все статусы, поиск — как у очереди."""
    return await requests_api.list_requests_sent_by_services(
        staff.service_keys,
        limit=limit,
        room_number=search.room_number,
        daily_number=search.daily_number,
    )


def read_only(card: dict[str, Any]) -> dict[str, Any]:
    """Карточка без действий: ни кнопок в строке и панели, ни форм."""
    return card | dict.fromkeys(_ACTION_FLAGS, False) | {"transfer_options": [], "order": None}
