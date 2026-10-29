"""Запись исхода вызова, которую не прерывает отмена задачи (ADR-024 §5, #531).

Общая для `complete()`, `embed()` и `stream()`: клиент ушёл, Starlette отменил
обработчик или процесс останавливается — строка журнала и снятие резерва всё
равно доходят до БД. Без этого оплаченный вызов оставался невидим бюджету, а
резерв держал его до конца аренды.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Coroutine
from typing import Any

import anyio

from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)


async def finish_despite_cancel(write: Coroutine[Any, Any, Any], *, tenant_id: uuid.UUID) -> None:
    """Довести запись до конца, даже если задачу отменяют.

    Отмена бывает двух видов, и защищаться приходится от обоих (проверено
    пробой на asyncio и anyio 4.15): anyio (Starlette, отмена scope'а)
    повторяет отмену на каждом `await` — от неё прикрывает щит `CancelScope`;
    обычный `task.cancel()` asyncio щит anyio пропускает — от него прикрывает
    отдельная задача и `asyncio.shield`, которых ждём до конца. Задача создаётся
    в контексте тенанта: поток, закрытый сборщиком мусора, финализируется в
    чужом контексте, а журнал — тенантная таблица (P-4).

    Отмена, пойманная во время ожидания, не теряется — после записи поднимается
    снова. Сбой самой записи — WARNING, а не исключение: он не вправе подменить
    собой отмену или исход вызова (незакрытый резерв истечёт по аренде, ADR-017).
    """
    with tenant_context(tenant_id):
        task = asyncio.ensure_future(write)
    cancelled = False
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
    if task.exception() is not None:
        logger.warning("llm_call_outcome_write_failed", exc_info=task.exception())
    if cancelled:
        raise asyncio.CancelledError
