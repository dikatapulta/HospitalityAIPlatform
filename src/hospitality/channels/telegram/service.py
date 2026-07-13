"""Обработка входящего вебхука Telegram (Task 0016, §7.1-вход, P-4, P-7, P-8).

Оркестрация приёма: нормализация → маппинг чата на тенанта → идемпотентная
запись → ответ. Бизнес-логики нет. Вызов оркестратора (Task 0015) и создание
заявок подключаются в Task 0017 «сквозная сборка»: здесь канал доведён до
сохранённого Message — граница задачи (PHASE0: «ещё чуть-чуть» — новая задача).

Маппинг чата на тенанта (Phase 0): один бот обслуживает демо-тенанта, тенант
берётся по slug из окружения (`TELEGRAM_TENANT_SLUG`) — как сервисный токен в
`platform/auth.py`. Пер-чатовый маппинг (несколько отелей за одним ботом) — Phase 1.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hospitality.channels.base import MessageKind, NormalizedMessage
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.normalize import normalize_update
from hospitality.channels.telegram.schemas import TelegramUpdate
from hospitality.channels.telegram.store import (
    ensure_conversation,
    insert_inbound_message,
    record_outbound_message,
)
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Вежливый отказ на не-текст (Phase 0 разбирает только текст). Двуязычный: у демо
# 70% гостей — иностранцы (память guest-demographics), а язык гостя без вызова LLM
# здесь неизвестен. Язык-осознанный отказ — Phase 1 (по конфигу тенанта/оркестратору).
UNSUPPORTED_REPLY = (
    "Пока я понимаю только текстовые сообщения — напишите, пожалуйста, текстом. "
    "I can only read text messages for now — please send your request as text."
)


async def process_update(
    update: TelegramUpdate, *, sender: TelegramSender, correlation_id: str
) -> None:
    """Обработать одно обновление вебхука (после проверки секрета в router)."""
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

        logger.info(
            "telegram_message_stored",
            conversation_id=str(conversation_id),
            message_id=str(message_id),
            kind=normalized.kind.value,
        )
        if normalized.kind is MessageKind.UNSUPPORTED:
            await _reply(conversation_id, normalized, UNSUPPORTED_REPLY, sender, correlation_id)


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


async def _reply(
    conversation_id: uuid.UUID,
    inbound: NormalizedMessage,
    text: str,
    sender: TelegramSender,
    correlation_id: str,
) -> None:
    """Отправить ответ гостю и записать его как исходящий Message (best-effort).

    Отправка best-effort (§8): сбой сети логируется, но не роняет вебхук и не
    записывает недоставленный ответ — иначе история диалога соврала бы. Telegram
    повторит доставку апдейта; входящее к тому моменту дедуплицировано (P-8),
    так что второго ответа не будет — компромисс Phase 0, задокументирован.
    """
    try:
        sent_id = await sender.send_message(inbound.chat_id, text)
    except Exception as error:  # best-effort: сбой отправки не роняет приём вебхука
        logger.warning("telegram_send_failed", chat_id=inbound.chat_id, error=str(error))
        return
    await record_outbound_message(
        conversation_id, text, correlation_id, external_message_id=sent_id
    )
