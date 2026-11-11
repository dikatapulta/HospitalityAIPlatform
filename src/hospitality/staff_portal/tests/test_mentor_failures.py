"""Отказ хода ИИ сотруднику — что идёт в счёт линии ERR-OPS-021 (issue #1050).

Путь «поток → событие `error` → лог и счётчик» — `test_mentor_stream_faults.py`; здесь —
перечень кодов: отказ, которого сотрудник сам не исправит, — в счёт, лимит и устаревшая
страница — нет.
"""

from __future__ import annotations

import uuid

import pytest
from prometheus_client import REGISTRY

from hospitality.ai.gateway import api as gateway
from hospitality.modules.mentor import api as mentor_api
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.mentor_failures import error_location, expected_failure
from hospitality.staff_portal.mentor_limit import ERR_MENTOR_TURN_LIMIT

TENANT = uuid.uuid4()


def _count(flow: str, reason: str) -> float:
    labels = {"tenant_id": str(TENANT), "flow": flow, "reason": reason}
    return REGISTRY.get_sample_value("mentor_turn_failures_total", labels) or 0.0


def _fail(code: str, flow: str = "exam_question") -> None:
    with tenant_context(TENANT):
        expected_failure(flow, AppError(code=code, message="отказ", status_code=503))


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (gateway.ERR_AI_PURPOSE_NOT_CONFIGURED, "model_unset"),
        (gateway.ERR_AI_PROVIDER_TIMEOUT, "provider"),
        (gateway.ERR_AI_PROVIDER_ERROR, "provider"),
        (gateway.ERR_AI_INCOMPLETE_ANSWER, "provider"),
        (mentor_api.ERR_MENTOR_CODEX_MISSING, "codex_missing"),
    ],
)
def test_failure_the_employee_cannot_fix_is_counted(code: str, reason: str) -> None:
    """Незаданная модель отказывает до журнала шлюза — серия ERR-OPS-018 её не видит."""
    before = _count("exam_question", reason)

    _fail(code)

    assert _count("exam_question", reason) == before + 1


@pytest.mark.parametrize(
    "code",
    [
        gateway.ERR_AI_BUDGET_EXCEEDED,  # своя линия ERR-OPS-019
        ERR_MENTOR_TURN_LIMIT,  # свой лимит сотрудника
        mentor_api.ERR_MENTOR_NO_EXAM_QUESTION,  # вторая вкладка, устаревшая страница
        mentor_api.ERR_MENTOR_NO_QUIZ_QUESTION,
    ],
)
def test_limits_and_stale_page_are_not_counted(code: str) -> None:
    before = {
        reason: _count("quiz_check", reason)
        for reason in ("model_unset", "provider", "codex_missing", "exception")
    }

    _fail(code, flow="quiz_check")

    assert {reason: _count("quiz_check", reason) for reason in before} == before


def test_error_location_points_at_our_last_frame() -> None:
    def broken_step() -> None:
        raise RuntimeError("секретный текст")

    try:
        broken_step()
    except RuntimeError as error:
        where = error_location(error)

    assert where.startswith("staff_portal/tests/test_mentor_failures.py:")
    assert where.endswith(" broken_step")
    assert "секрет" not in where


def test_error_without_traceback_has_no_location() -> None:
    assert error_location(RuntimeError("x")) == "—"
