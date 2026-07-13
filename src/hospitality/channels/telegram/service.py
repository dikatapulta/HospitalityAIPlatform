"""Приём вебхука Telegram: диспетчер входящих (Task 0016/0017, P-4, P-7, P-8).

Оркестрация приёма без бизнес-логики: нормализация → маппинг чата на тенанта →
идемпотентная запись → развилка «гость / персонал». Что делать с сохранённым
сообщением, решают тонкие обработчики:

- гостевой чат → `guest.handle_guest_message` зовёт оркестратор (Task 0015) и
  отвечает гостю (сквозная сборка, Task 0017);
- staff-чат (`TELEGRAM_STAFF_CHAT_ID`) → `staff.handle_staff_message` трактует
  текст как команду закрытия заявки, а не как реплику гостю (ADR-011).

Идемпотентность (P-8) — общая для обеих веток: и команда персонала, и реплика
гостя проходят `insert_inbound_message`, поэтому повтор апдейта (тот же update_id)
не создаёт второй записи и второго эффекта.

Маппинг чата на тенанта (Phase 0): один бот обслуживает демо-тенанта по slug из
окружения (`TELEGRAM_TENANT_SLUG`). Пер-чатовый маппинг (несколько отелей за одним
ботом) — Phase 1.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hospitality.ai.gateway.api import LlmProvider
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.guest import handle_guest_message
from hospitality.channels.telegram.normalize import normalize_update
from hospitality.channels.telegram.schemas import TelegramUpdate
from hospitality.channels.telegram.staff import handle_staff_message
from hospitality.channels.telegram.store import ensure_conversation, insert_inbound_message
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)


async def process_update(
    update: TelegramUpdate,
    *,
    sender: TelegramSender,
    provider: LlmProvider | None,
    correlation_id: str,
) -> None:
    """Обработать одно обновление вебхука (после проверки секрета в router).

    `provider` переопределяют тесты (scripted-фейк); прод передаёт None → боевой
    Anthropic из настроек (тот же приём подмены, что у `sender`).
    """
    normalized = normalize_update(update)
    if normalized is None:
        # Не сообщение (edited_message, callback_query, …) — Phase 0 не ведёт.
        logger.info("telegram_update_ignored", update_id=update.update_id)
        return

    tenant_id = await _resolve_tenant()
    if tenant_id is None:
        # Маппинг чата на тенанта не разрешился — ошибка окружения (сид/slug),
        # не гостя. Ответ 200 (не зацикливать ретраи Telegram), диагноз — в логах.
        return

    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation(normalized.chat_id)
        message_id = await insert_inbound_message(conversation_id, normalized, correlation_id)
        if message_id is None:
            # Повторная доставка того же update_id — второй Message не создаём (P-8).
            logger.info("telegram_duplicate_update", update_id=update.update_id)
            return

        is_staff = normalized.chat_id == get_settings().telegram_staff_chat_id
        logger.info(
            "telegram_message_stored",
            conversation_id=str(conversation_id),
            message_id=str(message_id),
            kind=normalized.kind.value,
            actor="staff" if is_staff else "guest",
        )
        if is_staff:
            await handle_staff_message(
                conversation_id, normalized, sender=sender, correlation_id=correlation_id
            )
        else:
            await handle_guest_message(
                conversation_id,
                normalized,
                message_id,
                sender=sender,
                provider=provider,
                correlation_id=correlation_id,
            )


async def _resolve_tenant() -> uuid.UUID | None:
    """Тенант канала по slug из окружения (маппинг чата, Phase 0)."""
    settings = get_settings()
    async with platform_session_scope() as session:
        tenant_id: uuid.UUID | None = await session.scalar(
            select(Tenant.id).where(Tenant.slug == settings.telegram_tenant_slug)
        )
    if tenant_id is None:
        logger.warning("telegram_tenant_missing", tenant_slug=settings.telegram_tenant_slug)
    return tenant_id
