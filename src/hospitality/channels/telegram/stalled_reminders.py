"""Напоминание о взятой и не закрытой заявке (issue #120, spec 0028 §8).

Пара `reminders.py`: там заявку никто не взял, здесь взяли и не закрыли —
например, забыли нажать «Готово», а гостю уже пообещали. Тот же прогон воркера,
тот же адресат (чат службы, `TenantConfig.staff_chat_for`), одно сообщение на
заявку — ключ `staff:request_stalled:<id>` на исходящем `Message` (P-8).

Отличия от невзятой — только данные: срок `TenantConfig.stalled_delay_for`
(по умолчанию выключен — долгая работа бывает нормой), отсчёт от взятия в
работу, кнопки «Готово» / «Отменить». Без LLM, как у пары.

Повторное взятие не напоминает второй раз: ключ живёт на заявке, а не на
взятии. Заявка, вернувшаяся в очередь, снова зовёт как невзятая (`reminders.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

import structlog

from hospitality.channels.common.store import (
    ensure_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.channels.telegram.reminders import (
    ERR_TELEGRAM_REMINDER_NOT_SENT,
    ERR_TELEGRAM_REMINDER_SCAN_FAILED,
    SCAN_LIMIT,
    humanized_age,
)
from hospitality.channels.telegram.routing import (
    ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED,
    category_name,
    current_correlation_id,
    log_routing,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig, list_configured_tenant_ids, load_tenant_config
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

IDEMPOTENCY_KEY_PREFIX: Final = "staff:request_stalled"


def taken_at(request: requests_api.ServiceRequestRead) -> datetime:
    """С какого момента заявка в работе: взятие, у старых строк — создание."""
    return request.claimed_at or request.created_at


async def remind_stalled_requests(*, sender: TelegramSender, default_staff_chat_id: str) -> int:
    """Один прогон по всем тенантам; вернуть число ушедших напоминаний.

    Сбой одного отеля не отменяет остальных (канон `remind_unclaimed_requests`).
    """
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        async with platform_session_scope() as session:
            tenant_ids = await list_configured_tenant_ids(session)

        candidates = 0
        reminded = 0
        for tenant_id in tenant_ids:
            try:
                tenant_candidates, tenant_reminded = await _remind_tenant(
                    tenant_id, sender=sender, default_staff_chat_id=default_staff_chat_id
                )
            except Exception:
                logger.error(
                    "stalled_request_scan_failed",
                    error_code=ERR_TELEGRAM_REMINDER_SCAN_FAILED,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
                continue
            candidates += tenant_candidates
            reminded += tenant_reminded
        logger.info(
            "stalled_requests_scanned",
            tenants=len(tenant_ids),
            candidates=candidates,
            reminded=reminded,
        )
        return reminded


async def _remind_tenant(
    tenant_id: uuid.UUID, *, sender: TelegramSender, default_staff_chat_id: str
) -> tuple[int, int]:
    with structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)):
        try:
            async with platform_session_scope() as session:
                config = await load_tenant_config(session, tenant_id)
        except AppError as error:
            logger.warning("stalled_request_scan_config_unavailable", error_code=error.code)
            return 0, 0

    delay = config.min_stalled_delay()
    if delay is None:
        return 0, 0  # у отеля выключено — заявки не читаем вовсе

    with tenant_context(tenant_id):
        candidates = await requests_api.list_stalled_requests(
            claimed_before=utc_now() - delay, limit=SCAN_LIMIT
        )
        if len(candidates) == SCAN_LIMIT:
            logger.warning("stalled_request_scan_truncated", limit=SCAN_LIMIT)
        categories = {category.id: category for category in await requests_api.list_categories()}

        reminded = 0
        now = utc_now()
        for request in candidates:
            category = categories.get(request.category_id)
            request_delay = config.stalled_delay_for(category.key if category else None)
            if request_delay is None or taken_at(request) + request_delay > now:
                continue  # в срез попала по чужому, более короткому сроку
            if await _remind_one(
                request,
                category=category,
                config=config,
                now=now,
                sender=sender,
                default_staff_chat_id=default_staff_chat_id,
            ):
                reminded += 1
        return len(candidates), reminded


async def _remind_one(
    request: requests_api.ServiceRequestRead,
    *,
    category: requests_api.RequestCategoryRead | None,
    config: TenantConfig,
    now: datetime,
    sender: TelegramSender,
    default_staff_chat_id: str,
) -> bool:
    """Напомнить об одной заявке; сбой отправки не пробрасывается (канон пары)."""
    staff_chat_id = config.staff_chat_for(
        category.key if category is not None else None, default=default_staff_chat_id
    )
    if not staff_chat_id:
        logger.warning(
            "telegram_staff_chat_not_configured",
            error_code=ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED,
            request_id=str(request.id),
        )
        return False

    idempotency_key = f"{IDEMPOTENCY_KEY_PREFIX}:{request.id}"
    if await notification_already_sent(idempotency_key):
        return False

    in_work = now - taken_at(request)
    text = stalled_text(request, category, humanized_age(in_work))
    try:
        conversation_id = await ensure_conversation(CHANNEL, staff_chat_id)
        sent_id = await sender.send_message(
            staff_chat_id,
            text,
            reply_markup=keyboards.keyboard_for_status(request.id, request.status),
        )
        await record_outbound_message(
            conversation_id,
            text,
            current_correlation_id(),
            external_message_id=sent_id,
            idempotency_key=idempotency_key,
        )
    except Exception:
        logger.error(
            "stalled_request_reminder_failed",
            error_code=ERR_TELEGRAM_REMINDER_NOT_SENT,
            request_id=str(request.id),
            exc_info=True,
        )
        return False

    logger.info(
        "stalled_request_reminded",
        request_id=str(request.id),
        daily_number=request.daily_number,
        in_work_minutes=int(in_work.total_seconds() // 60),
        category_key=category.key if category is not None else None,
        chat_id=staff_chat_id,
    )
    log_routing(staff_chat_id, category, default_staff_chat_id)
    return True


def stalled_text(
    request: requests_api.ServiceRequestRead,
    category: requests_api.RequestCategoryRead | None,
    in_work_label: str,
) -> str:
    """Текст персоналу без LLM: суть оригиналом гостя (канон `reminders.py`).

    Имени взявшего нет намеренно: оно ушло бы в Telegram и в `messages` — новый
    носитель ПД сотрудника (PII_REGISTRY), а кто взял, видно в кабинете.
    """
    number = f" #{request.daily_number}" if request.daily_number is not None else ""
    lines = [
        f"⏳ Заявка{number} в работе уже {in_work_label} и не закрыта",
        f"Категория: {category_name(category, request.category_id)}",
        f"Комната: {request.room_number or '—'}",
        f"Суть: {request.summary}",
    ]
    if request.daily_number is not None:
        action_line = f"Ход: /done {request.daily_number} · /cancel {request.daily_number}"
    else:
        action_line = f"id: {request.id}\nХод: /done · /cancel + этот id."
    return "\n".join([*lines, "", action_line])
