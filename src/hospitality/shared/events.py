"""Канонический слой доменных событий: outbox и доставка (Task 0010, ADR-005).

Как модули общаются побочными эффектами (P-6): бизнес-код публикует факт
(`DomainEvent`) через `publish()` В ТОЙ ЖЕ транзакции, что и бизнес-запись, —
строка попадает в таблицу `outbox_events` атомарно с данными. Отдельный
процесс-воркер (`hospitality.worker`) читает outbox и вызывает подписчиков.

Гарантии и обязанности (ADR-005):

- доставка at-least-once: событие не теряется при падении воркера, но может
  прийти повторно — каждый подписчик обязан быть идемпотентным (P-8);
- порядок доставки при сбоях не гарантируется — подписчик не должен
  полагаться на строгий порядок событий;
- подписчик выполняется в `tenant_context` тенанта события, correlation id
  публикации привязан к логам доставки (§10.2) — след «публикация → эффект»
  ищется по одному id.

Backoff между попытками и retention обработанных строк (issue #18, ADR-009):
неудачная доставка откладывает следующую попытку на `next_attempt_at`
(экспоненциально, `worker_retry_backoff_base_seconds`/`..._max_seconds`);
строки с `processed_at` старше `outbox_retention_days` периодически удаляются
`cleanup_processed_events()` из цикла воркера.

Канонический пример публикации (копируется каждым модулем):

    with tenant_context(tenant_id):
        async with session_scope() as session:
            session.add(service_request)
            await publish(session, RequestCreated(request_id=..., ...))

Канонический пример события и подписчика — `hospitality/platform/events.py`.
Подписчики регистрируются composition root'ом воркера (`hospitality/worker.py`).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, ClassVar, cast

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    CursorResult,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    delete,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.config import get_settings
from hospitality.shared.db import Base, UTCDateTime, platform_session_scope, utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id, tenant_context

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8) для лог-событий доставки.
ERR_EVENTS_DELIVERY_FAILED = "ERR-EVENTS-001"
ERR_EVENTS_DELIVERY_EXHAUSTED = "ERR-EVENTS-002"


class DomainEvent(BaseModel):
    """Базовый класс доменного события (GLOSSARY: «Доменное событие», P-7).

    Наследник объявляет `event_name` (канон имени: `<сущность>.<факт>`,
    например `request.created`) и типизированные поля полезной нагрузки.
    Событие неизменяемо: это уже случившийся факт, а не рабочий объект.
    """

    model_config = ConfigDict(frozen=True)

    event_name: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "event_name" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} обязан объявить event_name (ClassVar[str])")


# Реестр подписчиков процесса. Заполняется composition root'ом воркера
# (hospitality/worker.py) через subscribe(); API-процессу подписчики не нужны —
# он только публикует.
_subscribers: dict[str, list[Callable[[Any], Awaitable[None]]]] = {}
_event_types: dict[str, type[DomainEvent]] = {}


def subscribe[EventT: DomainEvent](
    event_type: type[EventT], handler: Callable[[EventT], Awaitable[None]]
) -> None:
    """Подписать обработчик на событие (вызывается composition root'ом воркера).

    Обработчик — `async def on_x(event: X) -> None`, обязан быть идемпотентным
    (P-8): повторный вызов с тем же событием не создаёт второй эффект.
    Повторная регистрация той же пары (событие, обработчик) безопасна.
    """
    event_name = event_type.event_name
    registered = _event_types.get(event_name)
    if registered is not None and registered is not event_type:
        raise ValueError(
            f"event_name {event_name!r} уже занят событием {registered.__name__}: "
            "имена событий уникальны в пределах платформы"
        )
    _event_types[event_name] = event_type
    handlers = _subscribers.setdefault(event_name, [])
    erased_handler = cast("Callable[[Any], Awaitable[None]]", handler)
    if erased_handler not in handlers:
        handlers.append(erased_handler)


class OutboxEvent(Base):
    """Строка outbox: опубликованный, но ещё не доставленный факт (P-6, ADR-005).

    Таблица тенантная (канон RLS 0002), но с дополнительной политикой
    `platform_dispatch` (миграция 0003): диспетчер воркера читает и помечает
    события ВСЕХ тенантов из платформенной сессии. Это единственное осознанное
    исключение из правила «платформенная сессия не видит тенантных таблиц» —
    оно не копируется в бизнес-таблицы.
    """

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    event_name: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB())
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    attempts: Mapped[int] = mapped_column(Integer(), default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text())
    # Backoff между попытками (issue #18, ADR-009): NULL — событие ещё не
    # пыталось доставляться (или уже доставлено) и берётся в работу немедленно;
    # после неудачи — момент, раньше которого диспетчер не возьмёт строку снова.
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


async def publish(session: AsyncSession, event: DomainEvent) -> None:
    """Опубликовать событие — в ТОЙ ЖЕ транзакции, что бизнес-запись (P-6).

    `session` — та же сессия, в которой пишется бизнес-запись: откат
    транзакции откатывает и событие (атомарность outbox). Требует
    `tenant_context` (P-4): tenant_id события берётся из контекста,
    подложить чужой нельзя.
    """
    correlation_value = structlog.contextvars.get_contextvars().get("correlation_id")
    session.add(
        OutboxEvent(
            tenant_id=current_tenant_id(),
            event_name=type(event).event_name,
            payload=event.model_dump(mode="json"),
            correlation_id=correlation_value if isinstance(correlation_value, str) else None,
        )
    )
    logger.info("event_published", event_name=type(event).event_name)


async def deliver_pending_events(
    batch_size: int | None = None,
    max_attempts: int | None = None,
    backoff_base_seconds: float | None = None,
    backoff_max_seconds: float | None = None,
) -> int:
    """Одна итерация диспетчера: забрать пачку недоставленных событий и доставить.

    Возвращает число взятых в работу событий (0 — outbox пуст ИЛИ все
    оставшиеся события ждут своего `next_attempt_at`, воркер может спать —
    этим же и достигается минимальная пауза цикла при пачке, целиком
    завершившейся ошибками, ADR-009). Платформенная транзакция держит строки
    под `FOR UPDATE SKIP LOCKED` до конца пачки: упавший процесс отпускает
    блокировки, и события доставит следующий запуск (at-least-once). Успех
    помечается `processed_at`; ошибка обработчика — `attempts`+1, `last_error`
    и `next_attempt_at` (экспоненциальный backoff), событие остаётся в очереди
    до `max_attempts` (дальше — разбор по ERR-EVENTS-002).
    """
    settings = get_settings()
    if batch_size is None:
        batch_size = settings.worker_batch_size
    if max_attempts is None:
        max_attempts = settings.worker_max_delivery_attempts
    if backoff_base_seconds is None:
        backoff_base_seconds = settings.worker_retry_backoff_base_seconds
    if backoff_max_seconds is None:
        backoff_max_seconds = settings.worker_retry_backoff_max_seconds

    now = utc_now()
    async with platform_session_scope() as session:
        pending = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.processed_at.is_(None),
                        OutboxEvent.attempts < max_attempts,
                        or_(
                            OutboxEvent.next_attempt_at.is_(None),
                            OutboxEvent.next_attempt_at <= now,
                        ),
                    )
                    .order_by(OutboxEvent.occurred_at)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for outbox_event in pending:
            await _deliver_one(
                outbox_event, max_attempts, backoff_base_seconds, backoff_max_seconds
            )
    return len(pending)


async def _deliver_one(
    outbox_event: OutboxEvent,
    max_attempts: int,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
) -> None:
    """Доставить одно событие всем подписчикам; исход записать в его строку outbox."""
    restored_log_context: dict[str, str] = {}
    if outbox_event.correlation_id is not None:
        restored_log_context["correlation_id"] = outbox_event.correlation_id
    with (
        tenant_context(outbox_event.tenant_id),
        structlog.contextvars.bound_contextvars(**restored_log_context),
    ):
        handlers = _subscribers.get(outbox_event.event_name, [])
        try:
            if handlers:
                event = _event_types[outbox_event.event_name].model_validate(outbox_event.payload)
                for handler in handlers:
                    await handler(event)
        except Exception as error:  # обработчик упал — событие остаётся в outbox
            outbox_event.attempts += 1
            outbox_event.last_error = f"{type(error).__name__}: {error}"[:1000]
            exhausted = outbox_event.attempts >= max_attempts
            if not exhausted:
                # Экспоненциальный backoff (ADR-009): 1-я попытка — через
                # base секунд, 2-я — 2*base, ... до потолка backoff_max_seconds.
                delay_seconds = min(
                    backoff_base_seconds * (2 ** (outbox_event.attempts - 1)),
                    backoff_max_seconds,
                )
                outbox_event.next_attempt_at = utc_now() + timedelta(seconds=delay_seconds)
            logger.error(
                "event_delivery_exhausted" if exhausted else "event_delivery_failed",
                error_code=(
                    ERR_EVENTS_DELIVERY_EXHAUSTED if exhausted else ERR_EVENTS_DELIVERY_FAILED
                ),
                event_name=outbox_event.event_name,
                event_id=str(outbox_event.id),
                attempts=outbox_event.attempts,
                next_attempt_at=(
                    outbox_event.next_attempt_at.isoformat()
                    if outbox_event.next_attempt_at
                    else None
                ),
                exc_info=True,
            )
            return
        outbox_event.attempts += 1
        outbox_event.processed_at = utc_now()
        # Событие без подписчиков — валидный случай (P-6: подписчики опциональны);
        # handlers=0 в логе отличает его от настоящей доставки.
        logger.info(
            "event_delivered",
            event_name=outbox_event.event_name,
            event_id=str(outbox_event.id),
            handlers=len(handlers),
        )


async def cleanup_processed_events(retention_days: int | None = None) -> int:
    """Удалить строки outbox, доставленные более `outbox_retention_days` назад.

    Часть retention-политики outbox (issue #18, ADR-009, FOUNDATION §9:
    «таблицы с неограниченным ростом получают retention в момент создания»).
    Вызывается периодически из цикла воркера (`worker_cleanup_interval_seconds`),
    отдельная джоба/фреймворк не заводятся (NG-8). Событие, не дошедшее до
    `processed_at` (в очереди или исчерпавшее попытки — ERR-EVENTS-002), не
    трогается: удаление касается только успешно доставленных фактов.
    """
    settings = get_settings()
    if retention_days is None:
        retention_days = settings.outbox_retention_days
    cutoff = utc_now() - timedelta(days=retention_days)

    async with platform_session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(OutboxEvent).where(
                    OutboxEvent.processed_at.is_not(None),
                    OutboxEvent.processed_at < cutoff,
                )
            ),
        )
    deleted = result.rowcount or 0
    if deleted:
        logger.info("outbox_events_cleaned_up", deleted=deleted, retention_days=retention_days)
    return deleted
