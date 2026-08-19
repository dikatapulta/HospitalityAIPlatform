"""Контракт эскалации к человеку (spec 0022, issue #36, P-7).

Общий словарь трёх сторон, лист-модуль без зависимостей от оркестратора:
оркестратор кладёт `EscalationContext` в исход `NEEDS_HUMAN`; канал добавляет
свой случай (`llm_unavailable`, деградация §7.8) и публикует
`conversation.escalated`; подписчик уведомлений строит по контексту текст для
staff-чата. LLM здесь не вызывается — модуль в контракте 4 import-linter
числится обязанностью анатомии ai/ (мета-тест tests/test_import_contracts.py).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class EscalationReason(enum.StrEnum):
    """Почему гостю пообещали человека — контекст для staff-чата."""

    UNKNOWN_TOOL = "unknown_tool"  # модель вызвала инструмент, которого нет в реестре
    TOOL_EXECUTION_FAILED = "tool_execution_failed"  # исполнение упало (ERR-AI-004 и т.п.)
    LLM_UNAVAILABLE = "llm_unavailable"  # деградация §7.8 — ставит канал, не оркестратор
    # ЧП: статический перехват сработал ДО модели (spec 0034, issue #208).
    # Единственная причина, которая до оркестратора вообще не доходит.
    EMERGENCY = "emergency"


@dataclass(frozen=True)
class EscalationContext:
    """Что знает система в момент эскалации.

    `action_summary`/`room_number` — из аргументов инструмента, когда известны:
    на ходе подтверждения последняя реплика гостя — «да», и без сути персонал
    не поймёт, о чём просьба. `error_code` — код каталога для логов/диагноза.
    """

    reason: EscalationReason
    error_code: str
    tool_name: str | None = None
    action_summary: str | None = None
    room_number: str | None = None
