"""Забытые заявки — снимок для алертера (issue #1050, часть 4; ERR-OPS-025).

Сигнал кабинета (spec 0039 §3) зовёт персонал невзятой заявкой — но только из
открытой вкладки: закрытая или уснувшая вкладка на стойке молчит, и заявка ждёт,
пока кто-нибудь сам не заглянет в очередь. Об этом узнаёт команда: обновлятор
scrape `/metrics` (подключает `app.py`) публикует по тенанту, сколько невзятых
заявок ждут дольше срока команды и сколько ждёт самая старая из них.

Срок команды — spec 0018 §3, п. 8а:
- срочная или эскалация — `URGENT_TEAM_DELAY`: лестница зовёт ресепшен и
  менеджера с первой минуты (spec 0045 §4), гость ждёт человека;
- остальные — `TEAM_DELAY_FACTOR` сроков службы `d` — тот же
  `reminder_delay_for`, что у метки «просрочена» (P-12): через `d` после того,
  как лестница позвала ресепшен и менеджера. Срок выключен — `d` по умолчанию
  платформы: выключенные напоминания снимают ступени кабинета, а не страховку.

«Невзятая» — ровно `new`, как у сигнала и очереди. Тенант без конфига пропущен
(онбординг не завершён, служебный smoke): ни сроков, ни персонала. Сбой одного
тенанта убирает из снимка только его — для алертера это «не знаю» о нём.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import (
    DEFAULT_REQUEST_REMINDER_MINUTES,
    TenantConfig,
    list_configured_tenant_ids,
    load_tenant_config,
)
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import ForgottenRequests, set_forgotten_requests
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.ladder import RECEPTION_DELAY_FACTOR

logger = get_logger(module=__name__)

URGENT_TEAM_DELAY: Final = timedelta(minutes=10)
# На один срок `d` позже третьей ступени лестницы (ресепшен и менеджер — через `2d`).
TEAM_DELAY_FACTOR: Final = RECEPTION_DELAY_FACTOR + 1
_DEFAULT_DELAY: Final = timedelta(minutes=DEFAULT_REQUEST_REMINDER_MINUTES)
# Страховка от неограниченного скана (канон `list_unclaimed_requests`): у пилота
# невзятых единицы, двести разом — авария, и алерт о ней всё равно уйдёт.
SCAN_LIMIT: Final = 200


def _calls_at_once(request: requests_api.ServiceRequestRead) -> bool:
    """Срочная или эскалация: лестница сразу зовёт ресепшен и менеджера."""
    return request.is_urgent or request.origin is requests_api.ServiceRequestOrigin.ESCALATION


def team_delay(
    request: requests_api.ServiceRequestRead, category_key: str | None, config: TenantConfig
) -> timedelta:
    """Сколько невзятая заявка ждёт, прежде чем о ней узнает команда."""
    if _calls_at_once(request):
        return URGENT_TEAM_DELAY
    return TEAM_DELAY_FACTOR * (config.reminder_delay_for(category_key) or _DEFAULT_DELAY)


def _scan_floor(config: TenantConfig) -> timedelta:
    """Моложе этого заявка не забыта ни по одному сроку тенанта — граница выборки."""
    shortest = min(config.min_reminder_delay() or _DEFAULT_DELAY, _DEFAULT_DELAY)
    return min(URGENT_TEAM_DELAY, TEAM_DELAY_FACTOR * shortest)


def count_forgotten(
    requests: list[requests_api.ServiceRequestRead],
    category_keys: dict[uuid.UUID, str],
    config: TenantConfig,
    now: datetime,
) -> ForgottenRequests:
    """Забытые среди невзятых: срочные и эскалации отдельно, возраст самой старой."""
    urgent = regular = 0
    oldest = 0.0
    for request in requests:
        age = now - request.created_at
        if age < team_delay(request, category_keys.get(request.category_id), config):
            continue
        if _calls_at_once(request):
            urgent += 1
        else:
            regular += 1
        oldest = max(oldest, age.total_seconds())
    return ForgottenRequests(urgent=urgent, regular=regular, oldest_age_seconds=oldest)


async def _tenant_forgotten(tenant_id: uuid.UUID, now: datetime) -> ForgottenRequests:
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
    with tenant_context(tenant_id):
        unclaimed = await requests_api.list_unclaimed_requests(
            created_before=now - _scan_floor(config), limit=SCAN_LIMIT
        )
        categories = await requests_api.list_categories()
    category_keys = {category.id: category.key for category in categories}
    return count_forgotten(unclaimed, category_keys, config, now)


async def refresh_forgotten_request_metrics() -> None:
    """Забытые заявки каждого тенанта — в `/metrics` (обновлятор scrape, issue #1050)."""
    try:
        async with platform_session_scope() as session:
            tenant_ids = await list_configured_tenant_ids(session)
    except Exception:  # диагностический путь: /metrics обязан отдаваться и без БД
        logger.warning("forgotten_requests_unavailable", exc_info=True)
        set_forgotten_requests({})
        return
    now = utc_now()
    snapshot: dict[str, ForgottenRequests] = {}
    for tenant_id in tenant_ids:
        try:
            snapshot[str(tenant_id)] = await _tenant_forgotten(tenant_id, now)
        except Exception:  # один отель не отнимает снимок у остальных
            logger.warning(
                "forgotten_requests_tenant_unavailable", tenant_id=str(tenant_id), exc_info=True
            )
    set_forgotten_requests(snapshot)
