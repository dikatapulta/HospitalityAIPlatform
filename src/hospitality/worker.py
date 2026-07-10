"""Воркер доменных событий — composition root второго процесса (Task 0010, ADR-005).

Тот же кодовый образ, что и API-приложение, другая точка входа (§5.3):
`python -m hospitality.worker`. Цикл: забрать пачку из outbox
(`deliver_pending_events`), доставить подписчикам; пустой outbox — спать
`worker_poll_interval_seconds`. Ошибка итерации целиком (БД недоступна,
миграции ещё не применены) не роняет процесс: логируется с ERR-EVENTS-003
и повторяется — `make dev`/деплой не обязаны угадывать порядок старта.

Подписчики регистрируются здесь явно — это аналог include_router в app.py:
новый модуль добавляет свою пару (событие, обработчик) в register_subscribers.
"""

from __future__ import annotations

import asyncio

from hospitality.platform.events import CanaryCreated, echo_canary_created
from hospitality.shared.config import get_settings
from hospitality.shared.events import deliver_pending_events, subscribe
from hospitality.shared.logging import configure_logging, get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): итерация воркера упала целиком.
ERR_WORKER_ITERATION_FAILED = "ERR-EVENTS-003"


def register_subscribers() -> None:
    """Единственное место подключения подписчиков к событиям (P-12)."""
    subscribe(CanaryCreated, echo_canary_created)


async def run_worker(iterations: int | None = None) -> None:
    """Цикл воркера. `iterations` ограничивает число итераций (для тестов)."""
    register_subscribers()
    completed = 0
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
        if processed == 0:
            await asyncio.sleep(get_settings().worker_poll_interval_seconds)


def main() -> None:  # pragma: no cover — точка входа процесса; логика покрыта run_worker
    configure_logging(get_settings().log_level)
    logger.info("worker_started")
    asyncio.run(run_worker())


if __name__ == "__main__":  # pragma: no cover
    main()
