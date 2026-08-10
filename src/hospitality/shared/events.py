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

Backoff между попытками и retention терминальных строк (issue #18, ADR-009;
issue #133, ADR-015): неудачная доставка откладывает следующую попытку на
`next_attempt_at` (экспоненциально, `worker_retry_backoff_base_seconds`/
`..._max_seconds`); исчерпав попытки, событие получает `dead_lettered_at` —
терминальное состояние, о котором человеку говорит алерт
(`shared/outbox_alerts.py`); строки, доставленные или похороненные больше
`outbox_retention_days` назад, удаляет `cleanup_terminal_events()` из цикла
воркера.

Доставка идёт тремя шагами, и сеть — между транзакциями, а не внутри (issue
#134, ADR-016): короткая транзакция берёт пачку в работу (`_claim_batch`),
подписчики вызываются вне транзакций (`_deliver_one`), исход каждого события
пишет своя короткая транзакция (`_record_outcome`). От второго диспетчера
взятую строку защищает аренда `locked_until`, а не блокировка строки.

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
from dataclasses import dataclass
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
    and_,
    delete,
    or_,
    select,
    text,
    update,
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
    # Dead-letter (issue #133, ADR-015): момент, когда попытки исчерпаны и
    # событие больше НЕ берётся в работу. Терминальное состояние — факт в
    # строке, а не вычисление `attempts >= max_attempts` над сегодняшним
    # конфигом: предел попыток меняется, похороненное событие — нет.
    dead_lettered_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    # Момент, когда о похоронах сказали человеку (алерт ERR-EVENTS-002).
    # Отдельное поле, а не флаг в памяти воркера: воркер рестартует чаще, чем
    # интервал алерта, и без метки в БД повторял бы алерт после каждого деплоя.
    dead_letter_alerted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Аренда строки на время доставки (issue #134, ADR-016): NULL — строка
    # свободна; значение в будущем — событие сейчас доставляется, и другой
    # диспетчер его не возьмёт. Раньше эту роль играла блокировка `FOR UPDATE`,
    # которую транзакция держала весь сетевой вызов. Аренда снимается вместе с
    # записью исхода, а если процесс умер — по её истечении (at-least-once).
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime())


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


@dataclass(frozen=True)
class _ClaimedEvent:
    """Снимок строки outbox, взятой в работу: с ним доставка живёт вне транзакции.

    Копия, а не ORM-строка: между «прочитать пачку» и «зафиксировать исход» нет
    ни открытой транзакции, ни сессии, и правка объекта никуда бы не сохранилась
    (ADR-016). Всё, что нужно доставке, — здесь.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    event_name: str
    payload: dict[str, Any]
    correlation_id: str | None
    attempts: int


@dataclass(frozen=True)
class _DeliveryOutcome:
    """Исход доставки одного события — то, что уйдёт в его строку outbox.

    Заполненное поле записывается, пустое не трогается: успех не стирает
    диагноз прошлых неудач, неудача не трогает `processed_at`.
    """

    attempts: int
    processed_at: datetime | None = None
    last_error: str | None = None
    next_attempt_at: datetime | None = None
    dead_lettered_at: datetime | None = None


async def deliver_pending_events(
    batch_size: int | None = None,
    max_attempts: int | None = None,
    backoff_base_seconds: float | None = None,
    backoff_max_seconds: float | None = None,
    lease_seconds: float | None = None,
) -> int:
    """Одна итерация диспетчера: забрать пачку недоставленных событий и доставить.

    Возвращает число взятых в работу событий (0 — outbox пуст ИЛИ все
    оставшиеся события ждут своего `next_attempt_at` либо чужой аренды, воркер
    может спать — этим же и достигается минимальная пауза цикла при пачке,
    целиком завершившейся ошибками, ADR-009).

    Три шага, и сеть — между транзакциями (issue #134, ADR-016): короткая
    транзакция берёт пачку в работу, подписчики вызываются без единой открытой
    транзакции, исход каждого события пишет своя короткая транзакция. Успех
    помечается `processed_at`; ошибка обработчика — `attempts`+1, `last_error`
    и `next_attempt_at` (экспоненциальный backoff), событие остаётся в очереди
    до `max_attempts`, а исчерпав их — получает `dead_lettered_at` и выбывает
    из выборки навсегда (ERR-EVENTS-002, ADR-015). Падение процесса между
    доставкой и фиксацией исхода даёт повторную доставку, когда истечёт аренда,
    а не потерю события: обработчики обязаны быть идемпотентными (P-8).

    Выборка отсекает похороненные события по `dead_lettered_at IS NULL`, а не
    по `attempts < max_attempts`: понижение предела не должно выкидывать живые
    строки из очереди молча — они получат ещё одну попытку и, если она тоже
    провалится, честные похороны с алертом.
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
    if lease_seconds is None:
        lease_seconds = settings.worker_delivery_lease_seconds

    claimed = await _claim_batch(batch_size, lease_seconds)
    for claimed_event in claimed:
        outcome = await _deliver_one(
            claimed_event, max_attempts, backoff_base_seconds, backoff_max_seconds
        )
        await _record_outcome(claimed_event.id, outcome)
    return len(claimed)


async def _claim_batch(batch_size: int, lease_seconds: float) -> list[_ClaimedEvent]:
    """Шаг 1: короткая транзакция «прочитать пачку» — взять события в работу.

    `FOR UPDATE SKIP LOCKED` остаётся, но держится ровно на время самой отметки:
    он защищает не доставку (она снаружи), а отметку — от двух диспетчеров,
    выбравших одну строку одновременно. Дальше строку держит аренда
    `locked_until`: до её конца событие не попадёт в чужую пачку, а умерший
    процесс вернёт его в очередь тем, что аренда истечёт (ADR-016).
    """
    now = utc_now()
    async with platform_session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.processed_at.is_(None),
                        OutboxEvent.dead_lettered_at.is_(None),
                        or_(
                            OutboxEvent.next_attempt_at.is_(None),
                            OutboxEvent.next_attempt_at <= now,
                        ),
                        or_(
                            OutboxEvent.locked_until.is_(None),
                            OutboxEvent.locked_until <= now,
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
        lease_until = utc_now() + timedelta(seconds=lease_seconds)
        for row in rows:
            row.locked_until = lease_until
        return [
            _ClaimedEvent(
                id=row.id,
                tenant_id=row.tenant_id,
                event_name=row.event_name,
                payload=row.payload,
                correlation_id=row.correlation_id,
                attempts=row.attempts,
            )
            for row in rows
        ]


async def _deliver_one(
    claimed_event: _ClaimedEvent,
    max_attempts: int,
    backoff_base_seconds: float,
    backoff_max_seconds: float,
) -> _DeliveryOutcome:
    """Шаг 2: доставить событие подписчикам ВНЕ транзакции; вернуть исход значениями.

    Здесь живёт весь сетевой I/O доставки (Telegram, LLM внутри обработчиков) —
    и ни одной открытой транзакции, ни одного занятого соединения БД на это
    время (issue #134, ADR-016). Строку не трогает: её обновит `_record_outcome`.
    """
    restored_log_context: dict[str, str] = {}
    if claimed_event.correlation_id is not None:
        restored_log_context["correlation_id"] = claimed_event.correlation_id
    attempts = claimed_event.attempts + 1
    with (
        tenant_context(claimed_event.tenant_id),
        structlog.contextvars.bound_contextvars(**restored_log_context),
    ):
        handlers = _subscribers.get(claimed_event.event_name, [])
        try:
            if handlers:
                event = _event_types[claimed_event.event_name].model_validate(claimed_event.payload)
                for handler in handlers:
                    await handler(event)
        except Exception as error:  # обработчик упал — событие остаётся в outbox
            exhausted = attempts >= max_attempts
            next_attempt_at = None
            if not exhausted:
                # Экспоненциальный backoff (ADR-009): 1-я попытка — через
                # base секунд, 2-я — 2*base, ... до потолка backoff_max_seconds.
                # 2.0, а не 2: float-степень переполняется в inf (его срежет
                # min), а не в OverflowError при патологически большом пределе
                # попыток.
                delay_seconds = min(
                    backoff_base_seconds * (2.0 ** (attempts - 1)), backoff_max_seconds
                )
                next_attempt_at = utc_now() + timedelta(seconds=delay_seconds)
            logger.error(
                "event_delivery_exhausted" if exhausted else "event_delivery_failed",
                error_code=(
                    ERR_EVENTS_DELIVERY_EXHAUSTED if exhausted else ERR_EVENTS_DELIVERY_FAILED
                ),
                event_name=claimed_event.event_name,
                event_id=str(claimed_event.id),
                attempts=attempts,
                next_attempt_at=next_attempt_at.isoformat() if next_attempt_at else None,
                exc_info=True,
            )
            return _DeliveryOutcome(
                attempts=attempts,
                last_error=f"{type(error).__name__}: {error}"[:1000],
                next_attempt_at=next_attempt_at,
                # Терминальное состояние (ADR-015): строка выбывает из выборки
                # диспетчера навсегда, а `alert_dead_letter_events` расскажет о
                # ней человеку. Возврат в очередь — только руками по runbook.
                dead_lettered_at=utc_now() if exhausted else None,
            )
        # Событие без подписчиков — валидный случай (P-6: подписчики опциональны);
        # handlers=0 в логе отличает его от настоящей доставки.
        logger.info(
            "event_delivered",
            event_name=claimed_event.event_name,
            event_id=str(claimed_event.id),
            handlers=len(handlers),
        )
        return _DeliveryOutcome(attempts=attempts, processed_at=utc_now())


async def _record_outcome(event_id: uuid.UUID, outcome: _DeliveryOutcome) -> None:
    """Шаг 3: короткая транзакция «зафиксировать исход» — исход в строку outbox.

    Аренда снимается всегда: исход записан, строка больше не «в работе».
    Не доехала и эта транзакция — событие вернётся в очередь по истечении
    аренды и будет доставлено повторно (at-least-once, P-8).
    """
    values: dict[str, Any] = {"attempts": outcome.attempts, "locked_until": None}
    if outcome.processed_at is not None:
        values["processed_at"] = outcome.processed_at
    if outcome.last_error is not None:
        values["last_error"] = outcome.last_error
    if outcome.next_attempt_at is not None:
        values["next_attempt_at"] = outcome.next_attempt_at
    if outcome.dead_lettered_at is not None:
        values["dead_lettered_at"] = outcome.dead_lettered_at
        # Каждые похороны рассказываются заново: событие, возвращённое в очередь
        # руками после неудачного фикса, хоронится второй раз и обязано снова
        # дойти до человека. Держать этот инвариант текстом runbook («сбросьте
        # и это поле тоже») значило бы повторить issue #133 — молчание в момент,
        # когда сказать некому, кроме кода.
        values["dead_letter_alerted_at"] = None

    async with platform_session_scope() as session:
        await session.execute(
            update(OutboxEvent).where(OutboxEvent.id == event_id).values(**values)
        )


async def cleanup_terminal_events(retention_days: int | None = None) -> int:
    """Удалить строки outbox, завершившиеся более `outbox_retention_days` назад.

    Часть retention-политики outbox (issue #18, ADR-009, FOUNDATION §9:
    «таблицы с неограниченным ростом получают retention в момент создания»).
    Вызывается периодически из цикла воркера (`worker_cleanup_interval_seconds`),
    отдельная джоба/фреймворк не заводятся (NG-8).

    Терминальны два исхода, и оба живут по одному сроку (issue #133, ADR-015 —
    одна политика жизненного цикла): доставленное событие (`processed_at`) и
    похороненное (`dead_lettered_at`, о нём человеку уже сказал алерт). Событие,
    ещё стоящее в очереди на доставку, не трогается — ни одного исхода у него
    пока нет.
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
                    or_(
                        and_(
                            OutboxEvent.processed_at.is_not(None),
                            OutboxEvent.processed_at < cutoff,
                        ),
                        and_(
                            OutboxEvent.dead_lettered_at.is_not(None),
                            OutboxEvent.dead_lettered_at < cutoff,
                        ),
                    )
                )
            ),
        )
    deleted = result.rowcount or 0
    if deleted:
        logger.info("outbox_events_cleaned_up", deleted=deleted, retention_days=retention_days)
    return deleted
