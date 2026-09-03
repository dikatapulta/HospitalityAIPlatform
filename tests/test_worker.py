"""Тесты воркера (Task 0010, ADR-005): сквозная доставка и живучесть цикла.

Механика доставки (атомарность, ретраи, идемпотентность) — в test_events.py;
здесь — composition root воркера: регистрация подписчиков, полный путь
«публикация → outbox → цикл воркера → эффект подписчика» и устойчивость
цикла к инфраструктурным сбоям.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from hospitality.platform.models import Tenant, TenantIsolationCanary
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.heartbeat import WorkerHeartbeat, read_heartbeat_age_seconds
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.publish_demo_event import DEMO_TENANT_SLUG, publish_demo_event
from hospitality.worker import run_worker


async def test_worker_delivers_demo_event_end_to_end(canonical_database: None) -> None:
    """Тот же сценарий, что smoke на staging (runbook deploy): демо-публикация
    создаёт тенанта и канарейку, цикл воркера доставляет событие каноническому
    подписчику — появляется echo-строка того же тенанта."""
    correlation_id = await publish_demo_event()
    assert correlation_id

    await run_worker(iterations=1)

    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == DEMO_TENANT_SLUG))
    assert tenant_id is not None
    with tenant_context(tenant_id):
        async with session_scope() as session:
            echo_count = await session.scalar(
                select(func.count())
                .select_from(TenantIsolationCanary)
                .where(TenantIsolationCanary.note.like("echo:%"))
            )
    assert echo_count == 1


async def test_worker_iteration_survives_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Недоступная БД / неприменённые миграции не роняют процесс: итерация
    логирует ERR-EVENTS-003 и повторяется после паузы (см. worker.run_worker)."""

    async def broken_delivery(
        batch_size: int | None = None, max_attempts: int | None = None
    ) -> int:
        raise RuntimeError("db is down")

    monkeypatch.setattr("hospitality.worker.deliver_pending_events", broken_delivery)
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()


async def test_worker_runs_cleanup_when_interval_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retention-очистка outbox (issue #18, ADR-009) вызывается из цикла воркера,
    когда с прошлой попытки прошло не меньше worker_cleanup_interval_seconds."""
    calls = 0

    async def fake_cleanup(retention_days: int | None = None) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.cleanup_terminal_events", fake_cleanup)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=1)
    finally:
        get_settings.cache_clear()
    assert calls == 1


async def test_worker_survives_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-EVENTS-004 (docs/runbooks/errors.md): сбой retention-очистки логируется
    и не роняет цикл воркера — доставка продолжается на следующей итерации."""

    async def broken_cleanup(retention_days: int | None = None) -> int:
        raise RuntimeError("db is down")

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.cleanup_terminal_events", broken_cleanup)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()


async def test_worker_skips_cleanup_before_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Обратная сторона предыдущего теста: одна очистка на старте процесса
    (иначе частые рестарты воркера отменяли бы retention — ревью PR #19),
    дальше до истечения интервала очистка не вызывается на каждой итерации —
    иначе холостой DELETE бил бы БД ежесекундно."""
    calls = 0

    async def fake_cleanup(retention_days: int | None = None) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.cleanup_terminal_events", fake_cleanup)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=3)
    finally:
        get_settings.cache_clear()
    assert calls == 1  # только стартовая; итерации 2–3 внутри интервала


async def test_worker_runs_reminder_scan_when_interval_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Напоминания о невзятых заявках (issue #57, spec 0028) живут в цикле
    воркера тем же способом, что retention-очистка: отдельной джобы нет."""
    calls = 0

    async def fake_scan(*, sender: object, default_staff_chat_id: str) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.remind_unclaimed_requests", fake_scan)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_REMINDER_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=1)
    finally:
        get_settings.cache_clear()
    assert calls == 1


async def test_worker_skips_reminder_scan_before_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Один прогон на старте процесса (иначе частые рестарты воркера отменяли бы
    напоминания), дальше — не на каждой итерации: холостой скан бил бы БД."""
    calls = 0

    async def fake_scan(*, sender: object, default_staff_chat_id: str) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.remind_unclaimed_requests", fake_scan)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_REMINDER_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=3)
    finally:
        get_settings.cache_clear()
    assert calls == 1  # только стартовый; итерации 2–3 внутри интервала


async def test_worker_survives_reminder_scan_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-TELEGRAM-005 (docs/runbooks/errors.md): сбой прогона напоминаний
    логируется и не роняет цикл — доставка событий продолжается."""

    async def broken_scan(*, sender: object, default_staff_chat_id: str) -> int:
        raise RuntimeError("db is down")

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.remind_unclaimed_requests", broken_scan)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_REMINDER_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()


async def test_worker_runs_retention_when_interval_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ретеншн гостевых текстов (issue #42, spec 0032) живёт в цикле воркера
    тем же способом, что очистка outbox: отдельной джобы нет."""
    calls = 0

    async def fake_retention(retention_days: int | None = None) -> object:
        nonlocal calls
        calls += 1
        return None

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.enforce_guest_text_retention", fake_retention)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_RETENTION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_REMINDER_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=1)
    finally:
        get_settings.cache_clear()
    assert calls == 1


async def test_worker_skips_retention_before_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Один прогон на старте процесса (иначе частые рестарты воркера отменяли бы
    ретеншн — ревью PR #19), дальше — не на каждой итерации: холостой обход
    тенантов бил бы БД."""
    calls = 0

    async def fake_retention(retention_days: int | None = None) -> object:
        nonlocal calls
        calls += 1
        return None

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.enforce_guest_text_retention", fake_retention)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_RETENTION_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=3)
    finally:
        get_settings.cache_clear()
    assert calls == 1  # только стартовый; итерации 2–3 внутри интервала


async def test_worker_survives_retention_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-CHANNEL-004 (docs/runbooks/errors.md): падение прогона ретеншна
    целиком логируется и не роняет цикл — доставка событий продолжается."""

    async def broken_retention(retention_days: int | None = None) -> object:
        raise RuntimeError("db is down")

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.enforce_guest_text_retention", broken_retention)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_RETENTION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()


async def test_worker_runs_daily_summary_when_interval_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Утреннее сообщение (issue #301, spec 0035 §8) живёт в цикле воркера тем же
    способом, что напоминания: планировщика нет (NG-8), «09:00 по времени отеля»
    проверяется внутри прогона."""
    calls: list[str] = []

    async def fake_summaries(*, sender: object, alert_sender: object, alert_chat_id: str) -> int:
        calls.append(alert_chat_id)
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.send_daily_summaries", fake_summaries)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_DAILY_SUMMARY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_REMINDER_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=1)
    finally:
        get_settings.cache_clear()
    assert calls == [""]  # чат команды из настроек; в тестах пара TELEGRAM_ALERT_* пуста


async def test_worker_skips_daily_summary_before_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Один прогон на старте процесса, дальше — не на каждой итерации: при
    периоде опроса в секунду холостой обход тенантов бил бы БД."""
    calls = 0

    async def fake_summaries(*, sender: object, alert_sender: object, alert_chat_id: str) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.send_daily_summaries", fake_summaries)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_DAILY_SUMMARY_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=3)
    finally:
        get_settings.cache_clear()
    assert calls == 1  # только стартовый; итерации 2–3 внутри интервала


async def test_worker_survives_daily_summary_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-TELEGRAM-007 (docs/runbooks/errors.md): падение прогона сводки целиком
    логируется и не роняет цикл — доставка событий продолжается."""

    async def broken_summaries(*, sender: object, alert_sender: object, alert_chat_id: str) -> int:
        raise RuntimeError("db is down")

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.send_daily_summaries", broken_summaries)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_DAILY_SUMMARY_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()


async def test_worker_reports_dead_letter_events_when_alerting_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #133: исчерпанная доставка доходит до человека из цикла воркера —
    тем же способом, что retention и напоминания (отдельной джобы нет)."""
    calls = 0

    async def fake_alert(send: object, *, batch_size: int | None = None) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.alert_dead_letter_events", fake_alert)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("WORKER_DEAD_LETTER_ALERT_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_CLEANUP_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=3)
    finally:
        get_settings.cache_clear()
    assert calls == 1  # стартовый прогон; итерации 2–3 внутри интервала


async def test_worker_skips_dead_letter_alerts_when_alerting_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без TELEGRAM_ALERT_* (dev, CI) шаг пропускается целиком: пометка «человеку
    сказано» не ставится, и события дождутся настроенного тракта алертов."""
    calls = 0

    async def fake_alert(send: object, *, batch_size: int | None = None) -> int:
        nonlocal calls
        calls += 1
        return 0

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.alert_dead_letter_events", fake_alert)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "")
    monkeypatch.setenv("WORKER_DEAD_LETTER_ALERT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)
    finally:
        get_settings.cache_clear()
    assert calls == 0


async def test_worker_survives_dead_letter_alert_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ERR-EVENTS-005 (docs/runbooks/errors.md): лежащий Telegram логируется и не
    роняет цикл — доставка событий важнее рассказа о похороненных."""

    async def broken_alert(send: object, *, batch_size: int | None = None) -> int:
        raise RuntimeError("telegram is down")

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.alert_dead_letter_events", broken_alert)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("TELEGRAM_ALERT_BOT_TOKEN", "alert-token")
    monkeypatch.setenv("TELEGRAM_ALERT_CHAT_ID", "-100777")
    monkeypatch.setenv("WORKER_DEAD_LETTER_ALERT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()


async def test_worker_marks_heartbeat_every_cycle(
    canonical_database: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #136: круг цикла оставляет отметку «я жив» — по её возрасту
    watchdog снаружи видит мёртвый воркер (о своей смерти процесс не доложит)."""
    async with platform_session_scope() as session:
        row = (await session.execute(select(WorkerHeartbeat))).scalar_one()
        row.beat_at = utc_now() - timedelta(hours=1)  # отметка миграции состарена

    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=1)
    finally:
        get_settings.cache_clear()

    age = await read_heartbeat_age_seconds()
    assert age is not None and age < 60


async def test_worker_skips_heartbeat_before_interval_elapses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пульс не пишется каждым кругом: при периоде опроса 1 с это была бы
    запись в БД каждую секунду ради сигнала, который читают раз в минуту."""
    calls = 0

    async def fake_heartbeat(name: str = "events-worker") -> None:
        nonlocal calls
        calls += 1

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.record_worker_heartbeat", fake_heartbeat)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=3)
    finally:
        get_settings.cache_clear()

    assert calls == 1  # стартовый круг; итерации 2–3 внутри интервала


async def test_worker_survives_heartbeat_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """ERR-OPS-005: недоступная БД не роняет цикл на пульсе — доставка событий
    важнее отметки о ней (и о недоступной БД уже говорит ERR-OPS-001)."""

    async def broken_heartbeat(name: str = "events-worker") -> None:
        raise RuntimeError("db is down")

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.record_worker_heartbeat", broken_heartbeat)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=2)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()
