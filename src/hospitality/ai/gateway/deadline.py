"""Потолок ожидания вызова шлюза (issue #271, spec 0055).

Без него худший ход гостя складывался из настроек — `LLM_MAX_ATTEMPTS ×
LLM_TIMEOUT_SECONDS` плюс паузы — и никто не проверял это как одно число.
Потолок задаёт вызывающий (гостевой канал знает, сколько гость готов ждать),
соблюдает шлюз: попытка обрывается в момент потолка, новая не начинается.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Deadline:
    """Момент по монотонным часам, после которого шлюз больше не ждёт провайдера.

    Один объект на ход: все вызовы шлюза этого хода делят одно время, поэтому
    второй вызов (классификатор подтверждения → новый запрос) получает только
    остаток, а не свежий потолок.
    """

    at: float  # time.monotonic()

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        return cls(at=time.monotonic() + seconds)

    def remaining_seconds(self) -> float:
        return self.at - time.monotonic()
