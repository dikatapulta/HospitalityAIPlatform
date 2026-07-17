"""Воркер доменных событий — composition root второго процесса (Task 0010, ADR-005).

Тот же кодовый образ, что и API-приложение, другая точка входа (§5.3):
`python -m hospitality.worker`. Цикл: забрать пачку из outbox
(`deliver_pending_events`), доставить подписчикам; пустой outbox (или все
оставшиеся события ждут `next_attempt_at` — backoff, ADR-009) — спать
`worker_poll_interval_seconds`. Ошибка итерации целиком (БД недоступна,
миграции ещё не применены) не роняет процесс: логируется с ERR-EVENTS-003
и повторяется — `make dev`/деплой не обязаны угадывать порядок старта.

Тот же цикл на старте процесса и дальше раз в `worker_cleanup_interval_seconds`
вызывает `cleanup_processed_events()` — retention-очистку доставленных строк
outbox (issue #18, ADR-009); отдельная джоба/расписание не заводятся (NG-8).

Подписчики регистрируются здесь явно — это аналог include_router в app.py:
новый модуль добавляет свою пару (событие, обработчик) в register_subscribers.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from hospitality.channels.telegram import notifications as telegram_notifications
from hospitality.channels.telegram.client import build_telegram_sender
from hospitality.platform.events import CanaryCreated, echo_canary_created
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.events import (
    cleanup_processed_events,
    deliver_pending_events,
    subscribe,
)
from hospitality.shared.logging import configure_logging, get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_WORKER_ITERATION_FAILED = "ERR-EVENTS-003"  # итерация воркера упала целиком
ERR_EVENTS_CLEANUP_FAILED = "ERR-EVENTS-004"  # retention-очистка outbox упала


def register_subscribers() -> None:
    """Единственное место подключения подписчиков к событиям (P-12).

    Новый потребитель события = строка здесь + обработчик в своём слое; модуль,
    публикующий событие, о подписчиках не знает (P-6). Аналог `include_router`.
    """
    subscribe(CanaryCreated, echo_canary_created)
    # Уведомления Telegram (Task 0017): служба узнаёт о новой заявке, гость —
    # о её выполнении. Отправитель и staff-чат берутся из настроек окружения.
    settings = get_settings()
    telegram_notifications.register(
        sender=build_telegram_sender(settings),
        staff_chat_id=settings.telegram_staff_chat_id,
    )


async def run_worker(iterations: int | None = None) -> None:
    """Цикл воркера. `iterations` ограничивает число итераций (для тестов)."""
    register_subscribers()
    completed = 0
    # Первая очистка — сразу на старте процесса: иначе воркер, рестартующий
    # чаще worker_cleanup_interval_seconds (частые деплои), не выполнит
    # retention ни разу (ревью PR #19, находка 1).
    last_cleanup_at = utc_now() - timedelta(seconds=get_settings().worker_cleanup_interval_seconds)
    while iterations is None or completed < iterations:
        completed += 1
        try:
            processed = await deliver_pending_events()
        except Exception:  # инфраструктурный сбой итерации — ждём и повторяем
            logger.error(
                "worker_iteration_failed",
                error_code=ERR_WORKER_ITERATION_FAILED,
                exc_info=True,
            )
            processed = 0

        now = utc_now()
        cleanup_interval = get_settings().worker_cleanup_interval_seconds
        if (now - last_cleanup_at).total_seconds() >= cleanup_interval:
            try:
                await cleanup_processed_events()
            except Exception:  # retention — не критичный путь доставки, не роняем цикл
                logger.error(
                    "outbox_cleanup_failed",
                    error_code=ERR_EVENTS_CLEANUP_FAILED,
                    exc_info=True,
                )
            last_cleanup_at = now

        if processed == 0:
            await asyncio.sleep(get_settings().worker_poll_interval_seconds)


def main() -> None:  # pragma: no cover — точка входа процесса; логика покрыта run_worker
    configure_logging(get_settings().log_level)
    logger.info("worker_started")
    asyncio.run(run_worker())


if __name__ == "__main__":  # pragma: no cover
    main()
