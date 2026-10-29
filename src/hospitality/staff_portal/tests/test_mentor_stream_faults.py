"""«Наставник»: сбои посреди потока ответа (#531).

**Уход сотрудника.** uvicorn 0.54 объявляет ASGI `spec_version` 2.3, и обычный
`StreamingResponse` Starlette отменяет обработчик в тот момент, когда сотрудник
ушёл: отмена бьёт в запрос к БД или вызов шлюза, SQLAlchemy пишет ERROR, а Sentry
получает событие с текстом вопроса в теле запроса. Тест зовёт приложение напрямую,
как uvicorn: `spec_version` 2.3, `receive` отдаёт `http.disconnect`, как только ход
дошёл до нужной фазы (эмбеддинг, запись диалога, резюме истории). Обещание на
каждой фазе: ни одной записи ERROR и ни одного события Sentry, открытых резервов
бюджета 0, у каждого вызова провайдера — строка журнала.

**Не-`AppError` посреди потока** — событие `error` «Наставник сейчас не отвечает»,
в логе только тип исключения. NUL в тексте и номер главы больше `Integer` — 422 до
потока: иначе ответ модели оплачен, а запись падает.
"""

# ruff: noqa: F811

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sentry_sdk.types import Event
from sqlalchemy import select
from starlette.types import Message
from structlog.testing import capture_logs

from hospitality.ai.gateway.api import MockEmbeddingProvider, MockLlmProvider
from hospitality.ai.gateway.models import LlmBudgetReservation, LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import LlmProviderEmbeddings, LlmProviderResult
from hospitality.ai.gateway.schemas import LlmRequest
from hospitality.ai.mentor.common import MentorProviders
from hospitality.app import create_app
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.models import StaffRole
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.mentor import UNAVAILABLE_TEXT
from hospitality.staff_portal.mentor_api import get_mentor_providers
from hospitality.staff_portal.tests.conftest import PortalHotel, submit_login
from hospitality.staff_portal.tests.test_mentor import (  # noqa: F401  (фикстура-реимпорт)
    API,
    _events,
    _hotel,
    _mentor_models,
)
from tests.test_sentry import captured_sentry_events  # noqa: F401  (фикстура-реимпорт)
from tests.test_staff_auth import create_staff_user, unique_login

QUESTION = "Гость Иванов из 512 жалуется на шум, что делать?"
ANSWER = "Извинитесь и предложите другой номер [Статья 2]."
# Ответ длиннее бюджета истории: второй вопрос запускает резюме (HISTORY_BUDGET_TOKENS).
LONG_ANSWER = "Извинитесь перед гостем и предложите другой номер [Статья 2]. " * 140
SUMMARY = "Резюме прошлого разговора."

OK, ABORTED = LlmCallStatus.OK, LlmCallStatus.ABORTED


class _EmbeddingsThenLeave(MockEmbeddingProvider):
    """Уход приходит, пока провайдер считает вектор вопроса."""

    def __init__(self, left: anyio.Event) -> None:
        super().__init__()
        self._left = left

    async def embed(self, texts: list[str]) -> LlmProviderEmbeddings:
        self._left.set()
        await anyio.sleep(0.05)
        return await super().embed(texts)


class _SummaryThenLeave(MockLlmProvider):
    """Уход приходит, пока модель пересказывает старую историю."""

    def __init__(self, left: anyio.Event) -> None:
        super().__init__(text=SUMMARY)
        self._left = left

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        self._left.set()
        await anyio.sleep(0.05)
        return await super().complete(request)


@dataclass(frozen=True)
class _Employee:
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    cookie: str


@pytest.fixture
async def app(captured_sentry_events: list[Event]) -> AsyncIterator[FastAPI]:
    """Приложение после `init_sentry` (канон `tests/test_sentry.py`): интеграции
    Starlette инструментируют его, и событие получило бы тело запроса."""
    yield create_app()


@pytest.fixture
async def employee(app: FastAPI, portal_hotel: PortalHotel) -> _Employee:
    """Сотрудник с кодексом отеля, вошедший в кабинет: cookie — для сырого ASGI."""
    await _hotel(portal_hotel.tenant_id)
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login, tenant_id=portal_hotel.tenant_id, role=StaffRole.STAFF, display_name="Әсел"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        await submit_login(client, staff_login)
        cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
    return _Employee(tenant_id=portal_hotel.tenant_id, user_id=user_id, cookie=cookie)


def _answer_with(app: FastAPI, fakes: MentorProviders) -> None:
    app.dependency_overrides[get_mentor_providers] = lambda: fakes


def _fakes(
    *,
    answer: str = ANSWER,
    embeddings: MockEmbeddingProvider | None = None,
    chat: MockLlmProvider | None = None,
) -> MentorProviders:
    return MentorProviders(
        chat=chat or MockLlmProvider(text=SUMMARY),
        streaming=MockLlmProvider(text=answer),
        embeddings=embeddings or MockEmbeddingProvider(),
    )


async def _ask(app: FastAPI, employee: _Employee, *, left: anyio.Event) -> list[Message]:
    """POST «Спросить», как его зовёт uvicorn: ASGI 2.3, `http.disconnect` — по `left`."""
    body = json.dumps({"message": QUESTION}).encode()
    request_sent = False
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await left.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    path = f"{API}/ask"
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
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
    return sent


async def _journal(employee: _Employee) -> tuple[list[LlmCallStatus], list[LlmCallLog], int]:
    """Статусы вызовов шлюза по порядку, строки `aborted` и число открытых резервов."""
    with tenant_context(employee.tenant_id):
        async with session_scope() as session:
            rows = list(await session.scalars(select(LlmCallLog).order_by(LlmCallLog.created_at)))
            reservations = list(await session.scalars(select(LlmBudgetReservation)))
    aborted = [row for row in rows if row.status is ABORTED]
    return [row.status for row in rows], aborted, len(reservations)


async def _conversation(employee: _Employee) -> mentor_api.ConversationRead | None:
    with tenant_context(employee.tenant_id):
        return await mentor_api.get_conversation(employee.user_id, mentor_api.ConversationMode.ASK)


def _assert_quiet(caplog: pytest.LogCaptureFixture, sentry: list[Event]) -> None:
    errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors == []
    assert sentry == []


async def test_leaving_during_embedding_stops_the_answer_at_first_word(
    app: FastAPI,
    employee: _Employee,
    captured_sentry_events: list[Event],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Вектор вопроса досчитан и записан; поток модели оборван (`aborted` со
    стоимостью резерва); оборванный ответ не запоминается (ADR-024 §5)."""
    left = anyio.Event()
    _answer_with(app, _fakes(embeddings=_EmbeddingsThenLeave(left)))

    with caplog.at_level(logging.WARNING):
        await _ask(app, employee, left=left)

    _assert_quiet(caplog, captured_sentry_events)
    # Кодекс при загрузке, вектор вопроса, поток ответа.
    statuses, aborted, reservations = await _journal(employee)
    assert statuses == [OK, OK, ABORTED]
    assert aborted[0].cost_usd > 0
    assert reservations == 0
    assert await _conversation(employee) is None


async def test_leaving_during_dialog_write_keeps_the_shown_answer(
    app: FastAPI,
    employee: _Employee,
    captured_sentry_events: list[Event],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ответ уже показан целиком, уход — посреди записи диалога: запись доходит до конца."""
    left = anyio.Event()
    _answer_with(app, _fakes())
    original: Callable[..., Awaitable[mentor_api.ConversationRead]] = mentor_api.append_messages

    async def append_then_leave(*args: Any, **kwargs: Any) -> mentor_api.ConversationRead:
        left.set()  # уход приходит посреди запроса к БД
        return await original(*args, **kwargs)

    monkeypatch.setattr(mentor_api, "append_messages", append_then_leave)

    with caplog.at_level(logging.WARNING):
        await _ask(app, employee, left=left)

    _assert_quiet(caplog, captured_sentry_events)
    statuses, _, reservations = await _journal(employee)
    assert statuses == [OK, OK, OK]
    assert reservations == 0
    conversation = await _conversation(employee)
    assert conversation is not None
    assert [message.text for message in conversation.messages] == [QUESTION, ANSWER]


async def test_leaving_during_summary_finishes_it(
    app: FastAPI,
    employee: _Employee,
    captured_sentry_events: list[Event],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Уход, пока модель пересказывает старую историю: резюме записано, вызов — в журнале."""
    _answer_with(app, _fakes(answer=LONG_ANSWER))
    await _ask(app, employee, left=anyio.Event())  # первый вопрос — дождался ответа
    left = anyio.Event()
    _answer_with(app, _fakes(answer=LONG_ANSWER, chat=_SummaryThenLeave(left)))

    with caplog.at_level(logging.WARNING):
        await _ask(app, employee, left=left)

    _assert_quiet(caplog, captured_sentry_events)
    # Кодекс; первый вопрос — вектор и поток; второй — вектор, поток и резюме.
    statuses, _, reservations = await _journal(employee)
    assert statuses == [OK] * 6
    assert reservations == 0
    conversation = await _conversation(employee)
    assert conversation is not None and conversation.summary == SUMMARY


def _client(app: FastAPI, employee: _Employee) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        headers={"cookie": employee.cookie, "origin": "https://test"},
    )


async def test_unexpected_error_mid_stream_is_error_event_without_text_in_logs(
    app: FastAPI,
    employee: _Employee,
    captured_sentry_events: list[Event],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Запись упала не `AppError` (как `DBAPIError` с параметрами запроса в тексте):
    сотруднику — «не отвечает», в логе — тип, а не текст; события Sentry нет."""

    async def broken_append(*args: Any, **kwargs: Any) -> mentor_api.ConversationRead:
        raise RuntimeError(f"insert failed [parameters: ({QUESTION!r},)]")

    _answer_with(app, _fakes())
    monkeypatch.setattr(mentor_api, "append_messages", broken_append)

    with capture_logs() as logs, caplog.at_level(logging.WARNING):
        async with _client(app, employee) as client:
            response = await client.post(f"{API}/ask", json={"message": QUESTION})

    kind, data = _events(response.text)[-1]
    assert (kind, data) == ("error", {"code": "ERR-PLATFORM-001", "message": UNAVAILABLE_TEXT})
    [failed] = [entry for entry in logs if entry["event"] == "mentor_stream_failed"]
    assert failed["error_type"] == "RuntimeError" and failed["log_level"] == "warning"
    assert QUESTION not in json.dumps(logs, ensure_ascii=False, default=str)
    _assert_quiet(caplog, captured_sentry_events)


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/ask", {"message": "Как встретить гостя?\x00"}),
        ("/quiz/answer", {"message": "Поздороваться\x00"}),
        ("/quiz/new", {"text": "встреча\x00"}),
        ("/quiz/new", {"chapter_number": 2**31}),
    ],
    ids=["ask-nul", "answer-nul", "topic-nul", "chapter-over-integer"],
)
async def test_text_that_cannot_be_stored_is_422_before_the_stream(
    app: FastAPI, employee: _Employee, path: str, body: dict[str, Any]
) -> None:
    """NUL Postgres не хранит, номер главы больше `Integer` роняет запрос: ответ модели
    был бы оплачен, а запись — нет, и каждый повтор платил бы снова."""
    _answer_with(app, _fakes())
    async with _client(app, employee) as client:
        response = await client.post(f"{API}{path}", json=body)

    assert response.status_code == 422
    statuses, _, _ = await _journal(employee)
    assert statuses == [OK]  # только загрузка кодекса: до модели дело не дошло
