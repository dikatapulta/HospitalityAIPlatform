"""Рассылка пуша на телефоны персонала (spec 0050 §6, issue #477).

Воркер зовёт `run_push_sweep` своей задачей рядом с циклом, раз в
`WORKER_STAFF_PUSH_INTERVAL_SECONDS`, по образцу медленной полосы (#511):
зависшая служба пуша не держит доставку событий. Воркер — второй composition
root, и этот модуль — единственное, что он берёт из кабинета (узкое исключение
контракта 7 import-linter).

Проход по отелю:
1. подписанные сотрудники с живым членством (`staff_push_journal.load_recipients`);
2. `signal.build_signal` — раз на группу «уровень + службы»: у группы ответ
   сигнала один, адресаты — те же, что у вкладки кабинета (P-12);
3. у каждого сотрудника — что нового против журнала, как `signal.js`
   `compare()`; новое уходит на все его телефоны;
4. журнал записывается целиком: в нём остаётся только то, что зовёт сейчас.

Первое включение (ни один телефон сотрудника его не прошёл) — тихо, кроме
срочных (spec 0039 §3.3). Ключ пишется, если дошло хотя бы на один телефон или
телефонов не осталось; все отправки не дошли — повтор следующим проходом.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Final

import structlog

from hospitality.platform import staff_push, staff_push_journal
from hospitality.platform.staff_auth import StaffAudience
from hospitality.platform.staff_push import PushMessage, StaffPushSubscription
from hospitality.shared.logging import get_logger
from hospitality.shared.ports.web_push import PushDelivery, PushOutcome, web_push_or_none
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import push_texts
from hospitality.staff_portal.signal import SignalView, build_signal, signal_context

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_PUSH_SWEEP_FAILED: Final = "ERR-PUSH-005"
ERR_PUSH_AUTH_REJECTED: Final = "ERR-PUSH-006"

# Старше получаса уведомление не нужно: заявку видно в кабинете, а лестница
# позвала ресепшен (spec 0050 §6).
PUSH_TTL_SECONDS: Final = 30 * 60
_MAX_PARALLEL_SENDS: Final = 8
# Ответы, которые говорят о подписи сервера, а не о телефоне (ключ VAPID, часы).
_AUTH_STATUSES: Final = frozenset({401, 403})


@dataclass(frozen=True)
class _Call:
    """Пункт сигнала для одного сотрудника: все его ключи сейчас и что слать."""

    keys: frozenset[str]
    message: PushMessage | None
    is_urgent: bool = False


@dataclass
class SweepStats:
    delivered: int = 0
    failed: int = 0
    auth_failed: int = 0
    deleted: int = 0
    tenants_failed: int = 0


@dataclass
class _Sender:
    semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(_MAX_PARALLEL_SENDS)
    )
    stats: SweepStats = field(default_factory=SweepStats)


# Сбой, который повторяется каждый проход (раз в 15 с), — ERROR в Sentry один
# раз на эпизод, дальше WARNING: иначе за час простоя — 240 одинаковых событий.
_failing_now: set[str] = set()


def _log_failure(key: str, event: str, **fields: object) -> None:
    if key in _failing_now:
        logger.warning(event, repeated=True, **fields)
        return
    _failing_now.add(key)
    logger.error(event, **fields)


async def run_push_sweep() -> SweepStats:
    """Один проход рассылки по всем отелям; пуш выключен на контуре — ничего."""
    stats = SweepStats()
    if web_push_or_none() is None:
        return stats
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        try:
            tenants = await staff_push_journal.list_push_tenants()
        except Exception:
            _log_failure(
                "tenants",
                "staff_push.sweep_failed",
                error_code=ERR_PUSH_SWEEP_FAILED,
                exc_info=True,
            )
            stats.tenants_failed += 1
            return stats
        _failing_now.discard("tenants")
        sender = _Sender(stats=stats)
        for tenant in tenants:
            key = f"tenant:{tenant.id}"
            try:
                with (
                    structlog.contextvars.bound_contextvars(tenant_id=str(tenant.id)),
                    tenant_context(tenant.id),
                ):
                    await _sweep_tenant(tenant.slug, sender)
            except Exception:
                # Сбой у одного отеля не отменяет рассылку остальным.
                _log_failure(
                    key,
                    "staff_push.sweep_failed",
                    error_code=ERR_PUSH_SWEEP_FAILED,
                    tenant_id=str(tenant.id),
                    exc_info=True,
                )
                stats.tenants_failed += 1
                continue
            _failing_now.discard(key)
        _report(stats)
    return stats


def _report(stats: SweepStats) -> None:
    if stats.failed:
        # Одна строка на проход, а не на отправку (spec 0050 §9).
        logger.warning(
            "staff_push.unavailable",
            error_code=staff_push.ERR_PUSH_UNAVAILABLE,
            failed=stats.failed,
        )
    if stats.auth_failed:
        _log_failure(
            "auth",
            "staff_push.auth_rejected",
            error_code=ERR_PUSH_AUTH_REJECTED,
            failed=stats.auth_failed,
        )
    elif stats.delivered:
        _failing_now.discard("auth")
    if stats.delivered or stats.failed or stats.deleted:
        logger.info(
            "staff_push.sweep_finished",
            delivered=stats.delivered,
            failed=stats.failed,
            deleted=stats.deleted,
        )


async def _sweep_tenant(tenant_slug: str, sender: _Sender) -> None:
    recipients, deleted = await staff_push_journal.load_recipients()
    sender.stats.deleted += deleted
    previous = await staff_push_journal.load_announced()
    signals: dict[StaffAudience, SignalView] = {}
    for recipient in recipients:
        if recipient.audience not in signals:
            signals[recipient.audience] = await build_signal(recipient.audience)
    urls = signal_context(tenant_slug)
    results = await asyncio.gather(
        *(
            _announce(
                recipient,
                signals[recipient.audience],
                previous.get(recipient.user_id, frozenset()),
                urls,
                sender,
            )
            for recipient in recipients
        )
    )
    current = {recipient.user_id: keys for recipient, keys in zip(recipients, results, strict=True)}
    await staff_push_journal.save_announced(previous, current)
    await staff_push_journal.mark_primed(
        [row.id for recipient in recipients for row in recipient.subscriptions]
    )


def _calls(signal: SignalView, seen: frozenset[str], urls: dict[str, str]) -> list[_Call]:
    """Что зовёт сотрудника и что из этого новое — порядок проверок `signal.js` `compare()`."""
    calls: list[_Call] = []
    for item in signal.unclaimed:
        new_key = f"new:{item.id}:{item.category_id}"
        overdue_key, raised_key = f"overdue:{item.id}", f"raised:{item.id}"
        keys = {new_key}
        if item.is_overdue:
            keys.add(overdue_key)
        if item.is_raised:
            keys.add(raised_key)
        message: PushMessage | None = None
        if new_key not in seen:
            message = push_texts.new_request(item, urls["queue_url"])
        elif item.is_raised and raised_key not in seen:
            message = push_texts.raised_request(item, urls["queue_url"])
        elif item.is_overdue and overdue_key not in seen:
            message = push_texts.overdue_request(item, urls["queue_url"])
        calls.append(_Call(frozenset(keys), message, is_urgent=item.is_urgent))
    for call in signal.turnovers:
        key = f"call:{call.id}:{call.kind}"
        message = None if key in seen else push_texts.turnover_call(call, urls["turnovers_url"])
        calls.append(_Call(frozenset({key}), message))
    return calls


async def _announce(
    recipient: staff_push_journal.PushRecipient,
    signal: SignalView,
    announced: frozenset[str],
    urls: dict[str, str],
    sender: _Sender,
) -> frozenset[str]:
    """Разослать новое на телефоны сотрудника; вернуть его журнал после прохода."""
    live = list(recipient.subscriptions)
    keys: set[str] = set()
    for call in _calls(signal, announced, urls):
        message = call.message
        if message is not None and not recipient.primed and not call.is_urgent:
            message = None  # первое включение: звучат только срочные
        if message is None or await _deliver_to_all(live, message, sender):
            keys |= call.keys
        else:
            keys |= call.keys & announced  # не дошло никуда — повтор следующим проходом
    return frozenset(keys)


async def _deliver_to_all(
    live: list[StaffPushSubscription], message: PushMessage, sender: _Sender
) -> bool:
    """Отправить на все живые телефоны; True — дошло хоть куда-то или телефонов не осталось.

    Удалённые подписки (`gone`, `rejected`) выпадают из `live`: следующее
    уведомление прохода на них уже не идёт.
    """

    async def send(row: StaffPushSubscription) -> tuple[StaffPushSubscription, PushDelivery]:
        async with sender.semaphore:
            delivery = await staff_push.deliver(
                row, message, ttl_seconds=PUSH_TTL_SECONDS, log_failure=False
            )
        return row, delivery

    results = await asyncio.gather(*(send(row) for row in live))
    delivered = False
    failed = False
    for row, delivery in results:
        if delivery.outcome is PushOutcome.DELIVERED:
            delivered = True
            sender.stats.delivered += 1
        elif delivery.outcome is PushOutcome.FAILED:
            failed = True
            sender.stats.failed += 1
            if delivery.status_code in _AUTH_STATUSES:
                sender.stats.auth_failed += 1
        else:
            live.remove(row)
            sender.stats.deleted += 1
    return delivered or not failed
