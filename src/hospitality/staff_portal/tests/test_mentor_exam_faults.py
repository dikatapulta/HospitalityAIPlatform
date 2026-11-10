"""Экзамен «Наставника»: обрыв соединения после показа ничего не отменяет (#966).

Раньше вопрос и оценка писались после конца потока: сотрудник видел «Оценка: неверно»,
рвал соединение — и отвечал на тот же вопрос снова, а оборванный `/exam/next` перебирал
вопросы. Теперь записи — до показа. Приложение зовётся сырым ASGI, как его зовёт uvicorn
(`spec_version` 2.3, `http.disconnect` — техника `test_mentor_stream_faults.py`).
"""

# ruff: noqa: F811

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from sentry_sdk.types import Event
from starlette.types import Message

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.ai.gateway.provider import LlmProviderStreamEnd
from hospitality.ai.gateway.schemas import LlmRequest
from hospitality.ai.mentor.common import MentorProviders
from hospitality.modules.mentor import api as mentor_api
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.tests.test_mentor import (  # noqa: F401  (фикстура-реимпорт)
    API,
    _events,
    _mentor_models,
)
from hospitality.staff_portal.tests.test_mentor_stream_faults import (  # noqa: F401
    ABORTED,
    OK,
    _answer_with,
    _assert_quiet,
    _client,
    _Employee,
    _journal,
    app,
    employee,
)
from tests.test_sentry import captured_sentry_events  # noqa: F401  (фикстура-реимпорт)

QUESTION = "Гость подходит, пока вы говорите по телефону. Что вы сделаете?"
VERDICT = "Оценка: неверно\nКак правильно: закончить звонок и поздороваться [Статья 2]."


class _QuestionThenLeave(MockLlmProvider):
    """Сотрудник уходит, пока модель пишет вопрос."""

    def __init__(self, left: anyio.Event) -> None:
        super().__init__(text=QUESTION)
        self._left = left

    async def stream(self, request: LlmRequest) -> AsyncGenerator[str | LlmProviderStreamEnd, None]:
        self._left.set()
        await anyio.sleep(0.05)
        async for item in super().stream(request):
            yield item


def _streaming(fakes: MentorProviders) -> MockLlmProvider:
    assert isinstance(fakes.streaming, MockLlmProvider)
    return fakes.streaming


def _with(app: FastAPI, streaming: MockLlmProvider) -> MentorProviders:
    fakes = MentorProviders(streaming=streaming)
    _answer_with(app, fakes)
    return fakes


async def _raw_post(
    app: FastAPI,
    employee: _Employee,
    path: str,
    payload: dict[str, Any],
    *,
    left: anyio.Event,
    leave_on: str | None = None,
) -> list[str]:
    """POST, как его зовёт uvicorn: `http.disconnect` — по `left`; `leave_on` — уйти, как
    только этот текст дошёл до сотрудника. Отдаёт куски тела, которые он получил."""
    body = json.dumps(payload).encode()
    request_sent = False
    received: list[str] = []

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        chunk = message.get("body", b"").decode()
        received.append(chunk)
        if leave_on is not None and leave_on in chunk:
            left.set()
            await anyio.sleep(0.05)  # уход доходит до приложения раньше следующего куска

    full_path = f"{API}{path}"
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": full_path,
        "raw_path": full_path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"origin", b"https://test"),
            (b"content-type", b"application/json"),
            (b"cookie", employee.cookie.encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 443),
    }
    with anyio.fail_after(10):
        await app(scope, receive, send)
    return received


async def _started(app: FastAPI, employee: _Employee) -> dict[str, Any]:
    async with _client(app, employee) as client:
        response = await client.post(f"{API}/exam/start", json={})
    assert response.status_code == 200
    progress: dict[str, Any] = response.json()
    return progress


async def _attempt(employee: _Employee) -> mentor_api.ExamAttemptRead:
    with tenant_context(employee.tenant_id):
        attempt = await mentor_api.current_exam(employee.user_id)
    assert attempt is not None
    return attempt


async def test_leaving_after_wrong_keeps_it_and_the_turn_is_closed(
    app: FastAPI,
    employee: _Employee,
    captured_sentry_events: list[Event],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сотрудник увидел «Оценка: неверно» и ушёл посреди разбора: «неверно» засчитано,
    повтор того же хода — ERR-MENTOR-004, модель не зовётся."""
    progress = await _started(app, employee)
    _with(app, MockLlmProvider(text=QUESTION))
    async with _client(app, employee) as client:
        await client.post(f"{API}/exam/next", json={"attempt_id": progress["attempt_id"]})
    answer = {"message": "Промолчу.", "attempt_id": progress["attempt_id"], "turn": 1}
    checking = _with(app, MockLlmProvider(text=VERDICT))

    received = await _raw_post(
        app, employee, "/exam/answer", answer, left=anyio.Event(), leave_on="неверно"
    )

    assert "Как правильно" not in "".join(received)  # разбор не дошёл — ушёл раньше
    attempt = await _attempt(employee)
    assert (attempt.wrong_count, attempt.current_question) == (1, None)
    statuses, _, reservations = await _journal(employee)
    assert statuses[-1] is ABORTED and reservations == 0
    _assert_quiet(caplog, captured_sentry_events)

    async with _client(app, employee) as client:
        again = await client.post(f"{API}/exam/answer", json={**answer, "message": "Поздороваюсь."})
    kind, data = _events(again.text)[-1]
    assert (kind, data["code"]) == ("error", mentor_api.ERR_MENTOR_NO_EXAM_QUESTION)
    assert len(_streaming(checking).calls) == 1
    assert (await _attempt(employee)).wrong_count == 1


async def test_leaving_while_the_question_is_written_still_opens_it(
    app: FastAPI,
    employee: _Employee,
    captured_sentry_events: list[Event],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ушёл, пока модель писала вопрос: вопрос открыт и статья спрошена — обрывом
    `/exam/next` вопросы не перебрать; после обновления страницы он тот же."""
    progress = await _started(app, employee)
    left = anyio.Event()
    _with(app, _QuestionThenLeave(left))

    received = await _raw_post(
        app, employee, "/exam/next", {"attempt_id": progress["attempt_id"]}, left=left
    )

    assert QUESTION not in "".join(received)
    attempt = await _attempt(employee)
    assert (attempt.turn, attempt.current_question) == (1, QUESTION)
    assert len(attempt.asked_articles) == 1
    statuses, _, reservations = await _journal(employee)
    assert statuses[-1] is OK and reservations == 0  # вопрос дописан, вызов не оборван
    _assert_quiet(caplog, captured_sentry_events)

    async with _client(app, employee) as client:
        again = await client.post(f"{API}/exam/next", json={"attempt_id": progress["attempt_id"]})
    assert _events(again.text)[-1][1]["code"] == mentor_api.ERR_MENTOR_NO_EXAM_QUESTION
