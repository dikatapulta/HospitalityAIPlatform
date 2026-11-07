"""Гость напоминает о заявке — глазами кабинета (spec 0070 §5, §7, issue #834).

Одно правило подъёма и одни слова на сигнал, пуш, очередь и главную (P-12):
`is_guest_raised` — обёртка чистой `ladder.guest_raised` настройкой отеля и
сроками службы заявки, `badge_label` — метка карточки. Считает напоминания
модуль `requests` (`record_guest_reminder`), здесь — только чтение.
"""

from __future__ import annotations

from typing import Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig
from hospitality.shared.humanize import plural
from hospitality.staff_portal.ladder import guest_raised

# Строка «Требует внимания» на главной (spec 0070 §7).
HOME_LABEL: Final = "Гость напоминает"

_OPEN: Final = frozenset({requests_api.RequestStatus.NEW, requests_api.RequestStatus.IN_PROGRESS})


def is_guest_raised(
    request: requests_api.ServiceRequestRead,
    category_key: str | None,
    config: TenantConfig | None,
) -> bool:
    """Подняли ли заявку напоминания гостя к ресепшену и менеджеру.

    Закрытая не зовёт никого; тенант без конфига (онбординг не завершён,
    smoke-тенант) лестницы не имеет — как у метки «просрочена» (`queue.is_overdue`).
    """
    if config is None or request.status not in _OPEN:
        return False
    return guest_raised(
        reminder_count=request.guest_reminder_count,
        reminded_at=request.guest_reminded_at,
        created_at=request.created_at,
        claimed_at=request.claimed_at,
        is_claimed=request.status is requests_api.RequestStatus.IN_PROGRESS,
        raise_after=config.guest_reminder_raise_after,
        delay=config.reminder_delay_for(category_key),
        stalled_delay=config.stalled_delay_for(category_key),
    )


def badge_label(count: int) -> str | None:
    """Метка карточки очереди с первого напоминания: «Гость напомнил», «… 2 раза»."""
    if count < 1:
        return None
    if count == 1:
        return "Гость напомнил"
    return f"Гость напомнил {count} {plural(count, 'раз', 'раза', 'раз')}"
