"""Алерт о событиях, ушедших в dead-letter (issue #133, ADR-015).

Событие, исчерпавшее повторы доставки, помечается `dead_lettered_at`
(`shared/events.py`) и больше не берётся в работу: уведомление службе о заявке
гостя не случится никогда. В логах об этом есть ERROR `event_delivery_exhausted`,
но лог — не адресат: его никто не читает, пока не пойдёт искать причину. Поэтому
воркер раз в `worker_dead_letter_alert_interval_seconds` зовёт
`alert_dead_letter_events` — она докладывает о похороненных событиях в
Telegram-канал команды (ERR-EVENTS-002, канон текста — `shared/alerting.py`).

Порядок шагов — отправить, потом пометить: падение между ними повторит алерт
(шум), обратный порядок — потеряет его молча, а issue #133 ровно про молчание.
Сеть при этом не держит транзакцию (ср. issue #134): чтение, отправка и
пометка — три отдельных шага.

Одно сообщение на пачку, а не на событие: сломанный обработчик хоронит события
десятками, и посекундная лента алертов — та же тишина, только громкая. Детали
в тексте — по старейшему из похороненных: с него начинается разбор.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, update

from hospitality.shared.alerting import AlertSender, format_alert
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.events import ERR_EVENTS_DELIVERY_EXHAUSTED, OutboxEvent
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Сколько символов `last_error` попадает в алерт: диагноз виден сразу, а полный
# текст (до 1000 символов) остаётся в строке outbox и в логах.
_LAST_ERROR_LIMIT = 160


def _events_word(count: int) -> str:
    """«1 событие», «3 события», «11 событий» — счётная форма для текста алерта."""
    if 11 <= count % 100 <= 14:
        return "событий"
    last_digit = count % 10
    if last_digit == 1:
        return "событие"
    if 2 <= last_digit <= 4:
        return "события"
    return "событий"


def _describe(event: OutboxEvent) -> str:
    """Строка про одно похороненное событие: имя, короткий id, попытки, диагноз."""
    reason = (event.last_error or "причина не записана").replace("\n", " ")
    if len(reason) > _LAST_ERROR_LIMIT:
        reason = reason[: _LAST_ERROR_LIMIT - 1] + "…"
    return f"{event.event_name} (id {str(event.id)[:8]}, {event.attempts} попыток): {reason}"


def build_alert_text(events: list[OutboxEvent], *, environment: str, runbook_url: str) -> str:
    """Текст алерта ERR-EVENTS-002 по пачке похороненных событий (старейшее — первое)."""
    oldest = events[0]
    count = len(events)
    described = _describe(oldest)
    detail = f"{count} {_events_word(count)} в dead-letter. " + (
        f"Старейшее — {described}" if count > 1 else described
    )
    return format_alert(
        error_code=ERR_EVENTS_DELIVERY_EXHAUSTED,
        title="уведомление не доставлено, повторы исчерпаны",
        detail=detail,
        environment=environment,
        runbook_url=runbook_url,
        tenant=str(oldest.tenant_id),
        correlation_id=oldest.correlation_id or "—",
    )


async def alert_dead_letter_events(send: AlertSender, *, batch_size: int | None = None) -> int:
    """Рассказать человеку о новых dead-letter событиях; вернуть их число.

    Берёт похороненные события, о которых ещё не докладывали
    (`dead_letter_alerted_at IS NULL`), шлёт одно сообщение и метит их
    рассказанными. Кросс-тенантное чтение outbox — платформенной сессией, как в
    `deliver_pending_events` (единственное осознанное исключение, миграция 0003).

    Строки не блокируются `FOR UPDATE`: воркер в Фазе 0 один (ADR-005), а цена
    гонки — повторный алерт, а не потерянный. Сбой отправки пробрасывается
    вызывающему: пометка не ставится, пачка уйдёт следующим прогоном.
    """
    settings = get_settings()
    if batch_size is None:
        batch_size = settings.worker_batch_size

    async with platform_session_scope() as session:
        buried = list(
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.dead_lettered_at.is_not(None),
                        OutboxEvent.dead_letter_alerted_at.is_(None),
                    )
                    .order_by(OutboxEvent.dead_lettered_at)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        alerted_ids: list[uuid.UUID] = [event.id for event in buried]
        text = (
            build_alert_text(
                buried,
                environment=settings.sentry_environment,
                runbook_url=settings.alert_runbook_url,
            )
            if buried
            else ""
        )

    if not buried:
        return 0

    await send(text)

    async with platform_session_scope() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(alerted_ids))
            .values(dead_letter_alerted_at=utc_now())
        )
    logger.warning(
        "outbox_dead_letter_alerted",
        error_code=ERR_EVENTS_DELIVERY_EXHAUSTED,
        events=len(alerted_ids),
        oldest_event_id=str(alerted_ids[0]),
    )
    return len(alerted_ids)
