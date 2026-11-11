"""Отказ хода ИИ сотруднику — лог и счётчик для алертера (issue #1050, ERR-OPS-021).

Ход «Наставника» и «Предложить идею» отвечает потоком: 200 уже ушёл, и отказ сотрудник
видит событием `error` «не отвечает». До #1050 такой отказ видели только логи:
неожиданная ошибка писалась на WARNING и в Sentry не попадала, а незаданная модель
(ERR-AI-006) отказывает до журнала шлюза — серия ERR-OPS-018 не растёт.

- **Неожиданная ошибка** (не `AppError`) — ERROR `mentor_stream_failed` с типом и местом
  (последний кадр нашего кода: файл, строка, функция), без текста исключения: текст
  ошибки БД несёт параметры запроса — текст сотрудника (PII_REGISTRY). Тело запроса
  в Sentry не уходит (`max_request_body_size="never"`, #433) — поэтому ERROR теперь можно.
- **Ожидаемый отказ** (`AppError`) — WARNING, как раньше. В счёт идут только отказы,
  которых сотрудник сам не исправит: модель не задана, провайдер не ответил, кодекс не
  загружен. Лимиты (свой — ERR-MENTOR-007, отеля — ERR-AI-002 со своей линией
  ERR-OPS-019) и устаревшая страница (вопрос уже не открыт) — не отказ ИИ.
"""

from __future__ import annotations

import traceback
from typing import Final

from hospitality.ai.gateway import api as gateway
from hospitality.modules.mentor import api as mentor_api
from hospitality.shared.errors import INTERNAL_ERROR_CODE, AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_mentor_turn_failure

logger = get_logger(module=__name__)

# Код отказа → причина в счётчике `mentor_turn_failures_total`; кода нет — не в счёт.
FAILURE_REASONS: Final[dict[str, str]] = {
    gateway.ERR_AI_PURPOSE_NOT_CONFIGURED: "model_unset",
    gateway.ERR_AI_PROVIDER_TIMEOUT: "provider",
    gateway.ERR_AI_PROVIDER_ERROR: "provider",
    gateway.ERR_AI_INCOMPLETE_ANSWER: "provider",
    mentor_api.ERR_MENTOR_CODEX_MISSING: "codex_missing",
}
EXCEPTION_REASON: Final = "exception"

_PACKAGE_MARK: Final = "/hospitality/"


def expected_failure(flow: str, error: AppError) -> None:
    """Ожидаемый отказ хода: WARNING и, если он из перечня, — счёт для алертера."""
    logger.warning("mentor_stream_failed", flow=flow, error_code=error.code)
    reason = FAILURE_REASONS.get(error.code)
    if reason is not None:
        record_mentor_turn_failure(flow, reason)


def unexpected_failure(flow: str, error: Exception) -> None:
    """Неожиданная ошибка хода: ERROR с типом и местом — событие Sentry — и счёт."""
    logger.error(
        "mentor_stream_failed",
        flow=flow,
        error_code=INTERNAL_ERROR_CODE,
        error_type=type(error).__name__,
        error_where=error_location(error),
    )
    record_mentor_turn_failure(flow, EXCEPTION_REASON)


def error_location(error: BaseException) -> str:
    """«modules/mentor/exams.py:204 record_exam_answer» — последний кадр нашего кода.

    Кадры библиотек (SQLAlchemy, httpx) пропускаются: место в них не говорит, какой шаг
    хода упал. Нашего кадра нет — последний кадр как есть.
    """
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return "—"
    ours = [frame for frame in frames if _PACKAGE_MARK in frame.filename]
    frame = (ours or frames)[-1]
    path = frame.filename.rpartition(_PACKAGE_MARK)[2] or frame.filename
    return f"{path}:{frame.lineno} {frame.name}"
