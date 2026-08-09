"""Ретеншн гостевых текстов (issue #42, spec 0032).

Опубликованная политика конфиденциальности обещает: тексты сообщений — 90 дней,
затем удаляются; свободный текст заявок обезличивается через 90 дней
(`docs/legal/privacy-policy.md` п. 7). Исполняет обещание этот модуль: воркер
раз в `worker_retention_interval_seconds` зовёт `enforce_guest_text_retention`,
которая по каждому тенанту:

1. удаляет `messages` старше `messages_retention_days`;
2. удаляет `conversations`, давно не обновлявшиеся И оставшиеся без сообщений
   (каскадом уходят `request_origins`; согласие гостя — доказательная запись
   «как у диалога», PII_REGISTRY — умирает вместе с ним: вернувшийся после
   90+ дней тишины гость проходит consent-gate заново, это корректно);
3. обезличивает свободный текст заявок того же возраста через публичный API
   модуля requests (домен сам правит свою таблицу, spec 0028 §3).

Механика — канон периодической задачи `cleanup_terminal_events` (ADR-009) и
канон обхода тенантов `remind_unclaimed_requests` (spec 0028 §4), но списком
ВСЕХ тенантов (`list_tenant_ids`): данные и юр-обязанность есть и у тенанта
без завершённого онбординга. Идемпотентность (P-8) — свойство самих операций:
повторный прогон не находит строк; гонка двух воркеров безопасна.

Мультитенантность (P-4): список тенантов берётся платформенной сессией, все
DELETE/UPDATE — внутри `tenant_context` каждого. Кросс-тенантного запроса к
бизнес-таблице здесь нет и быть не может.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, NamedTuple, cast

import structlog
from sqlalchemy import delete, exists
from sqlalchemy.engine import CursorResult

from hospitality.channels.common.models import Conversation, Message
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import list_tenant_ids
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): сбой ретеншна на одном
# тенанте и падение прогона целиком — один диагноз (инфраструктура/БД, как у
# ERR-EVENTS-004), поэтому один код, в отличие от пары напоминаний.
ERR_CHANNEL_RETENTION_FAILED = "ERR-CHANNEL-004"


class RetentionRunStats(NamedTuple):
    """Счётчики одного прогона — опора лога `guest_text_retention_completed`."""

    tenants: int
    messages_deleted: int
    conversations_deleted: int
    requests_anonymized: int


async def enforce_guest_text_retention(retention_days: int | None = None) -> RetentionRunStats:
    """Один прогон ретеншна по всем тенантам; вернуть счётчики.

    Зовётся из цикла воркера (`hospitality/worker.py`) и тестами напрямую.
    Без явного `retention_days` берётся `messages_retention_days` из Settings
    (сигнатура — копия канона `cleanup_terminal_events`). Прогон получает
    собственный `correlation_id` (§10.2, канон spec 0028). Сбой на одном
    тенанте логируется ERR-CHANNEL-004 и не отменяет остальных.
    """
    if retention_days is None:
        retention_days = get_settings().messages_retention_days
    cutoff = utc_now() - timedelta(days=retention_days)
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        async with platform_session_scope() as session:
            tenant_ids = await list_tenant_ids(session)

        totals = RetentionRunStats(len(tenant_ids), 0, 0, 0)
        for tenant_id in tenant_ids:
            try:
                deleted = await _enforce_tenant(tenant_id, cutoff)
            except Exception:
                # Сбой на одном отеле (битые данные, недоступная в этот момент
                # БД) не отменяет ретеншн у остальных: обещание политики дано
                # каждому гостю, а не «пока всё работает».
                logger.error(
                    "guest_text_retention_failed",
                    error_code=ERR_CHANNEL_RETENTION_FAILED,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
                continue
            totals = RetentionRunStats(
                totals.tenants,
                totals.messages_deleted + deleted.messages_deleted,
                totals.conversations_deleted + deleted.conversations_deleted,
                totals.requests_anonymized + deleted.requests_anonymized,
            )
        # Нулевые счётчики отличают «нечего удалять» от «джоба не ходила».
        logger.info(
            "guest_text_retention_completed",
            retention_days=retention_days,
            tenants=totals.tenants,
            messages_deleted=totals.messages_deleted,
            conversations_deleted=totals.conversations_deleted,
            requests_anonymized=totals.requests_anonymized,
        )
        return totals


async def _enforce_tenant(tenant_id: uuid.UUID, cutoff: datetime) -> RetentionRunStats:
    """Ретеншн одного тенанта: сообщения → диалоги → тексты заявок.

    Порядок важен: диалог, чьё последнее сообщение только что удалено,
    становится пустым и уходит в этом же прогоне (его `updated_at` не моложе
    последнего сообщения — запись сообщений `updated_at` не трогает).
    """
    with (
        structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)),
        tenant_context(tenant_id),
    ):
        async with session_scope() as session:
            messages_deleted = cast(
                "CursorResult[Any]",
                await session.execute(delete(Message).where(Message.created_at < cutoff)),
            ).rowcount
            # Живой диалог держат либо оставшиеся сообщения, либо свежий
            # updated_at (pending_action, только что данное согласие).
            conversations_deleted = cast(
                "CursorResult[Any]",
                await session.execute(
                    delete(Conversation).where(
                        Conversation.updated_at < cutoff,
                        ~exists().where(Message.conversation_id == Conversation.id),
                    )
                ),
            ).rowcount
        requests_anonymized = await requests_api.anonymize_expired_request_texts(
            created_before=cutoff
        )
    return RetentionRunStats(
        1, messages_deleted or 0, conversations_deleted or 0, requests_anonymized
    )
