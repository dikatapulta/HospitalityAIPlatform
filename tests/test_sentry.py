"""Task 0018: Sentry получает необработанные ошибки с контекстом (§10.4).

Транспорт подменяется in-memory перехватом — реальный DSN и сеть не нужны.
Порядок фикстур в сигнатуре важен: init_sentry должен отработать ДО
create_app, чтобы интеграции Starlette/FastAPI инструментировали приложение.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event
from starlette.datastructures import Headers
from starlette.types import Scope

from hospitality.app import create_app
from hospitality.shared.config import Settings
from hospitality.shared.errors import AppError
from hospitality.shared.sentry import add_context_tags, init_sentry

TEST_TENANT_ID = uuid.uuid4()


class CapturingTransport(Transport):
    """In-memory транспорт: события складываются в список вместо сети."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[Event] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(event)


@pytest.fixture
def captured_sentry_events() -> Iterator[list[Event]]:
    transport = CapturingTransport()
    init_sentry(
        Settings(sentry_dsn="https://public@sentry.invalid/1", sentry_environment="test"),
        transport=transport,
    )
    yield transport.events
    sentry_sdk.get_client().flush()
    # Глобальный клиент не должен протекать в другие тесты процесса.
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def sentry_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """Приложение настоящим composition root, но с фейковым резолвером тенанта:
    контекст тенанта устанавливает настоящий TenantContextMiddleware — тест
    проверяет тот же путь биндинга, что и на staging (без БД)."""

    async def fake_resolver(scope: Scope) -> uuid.UUID | None:
        if Headers(scope=scope).get("Authorization") == "Bearer sentry-test-token":
            return TEST_TENANT_ID
        return None

    monkeypatch.setattr("hospitality.app.resolve_tenant_from_service_token", fake_resolver)
    app = create_app()

    @app.get("/sentry-boom")
    async def sentry_boom() -> None:
        raise RuntimeError("sentry test explosion")

    @app.get("/sentry-expected")
    async def sentry_expected() -> None:
        raise AppError(code="ERR-TEST-001", message="expected error", status_code=418)

    return app


def test_unhandled_error_reaches_sentry_with_tenant_and_correlation_id(
    captured_sentry_events: list[Event], sentry_app: FastAPI
) -> None:
    client = TestClient(sentry_app, raise_server_exceptions=False)

    response = client.get("/sentry-boom", headers={"Authorization": "Bearer sentry-test-token"})
    sentry_sdk.get_client().flush()

    assert response.status_code == 500
    assert captured_sentry_events, "необработанная ошибка обязана породить событие"
    for event in captured_sentry_events:
        tags = event.get("tags", {})
        assert tags.get("tenant_id") == str(TEST_TENANT_ID)
        assert tags.get("correlation_id") == response.headers["X-Correlation-ID"]
        assert event.get("environment") == "test"


def test_expected_app_error_does_not_create_sentry_event(
    captured_sentry_events: list[Event], sentry_app: FastAPI
) -> None:
    """AppError — ожидаемая ошибка (§10.5): её диагностирует каталог, не трекер."""
    client = TestClient(sentry_app)

    response = client.get("/sentry-expected")
    sentry_sdk.get_client().flush()

    assert response.status_code == 418
    assert captured_sentry_events == []


def test_empty_dsn_leaves_sentry_disabled() -> None:
    init_sentry(Settings(sentry_dsn=""))

    assert not sentry_sdk.get_client().is_active()


def test_before_send_masks_secret_path_in_request_url() -> None:
    """URL запроса кладёт интеграция Starlette, и `send_default_pii=False` его не
    режет: токен bind-ссылки уехал бы в трекер живым (§11, ревью PR #155)."""
    token = "x7Kq9vLm2pR4tZ-live-credential"
    event: Event = {"request": {"url": f"https://necturn.com/w/hotel-astana/b/{token}"}}

    masked = add_context_tags(event, {})

    assert masked["request"]["url"] == "https://necturn.com/w/hotel-astana/b/***"


def test_before_send_leaves_ordinary_urls_intact() -> None:
    event: Event = {"request": {"url": "https://necturn.com/g/hotel-astana/101/messages"}}

    assert add_context_tags(event, {})["request"]["url"] == (
        "https://necturn.com/g/hotel-astana/101/messages"
    )


def test_before_send_masks_secret_path_in_referer() -> None:
    """Список чувствительных заголовков SDK (cookie, authorization, x-real-ip …)
    `referer` не содержит, а браузер шлёт в нём ПОЛНЫЙ адрес страницы: гость на
    экране согласия отдаёт свой токен и в запросе привязки, и при переходе по
    ссылке на политику (ревью PR #155)."""
    token = "x7Kq9vLm2pR4tZ-live-credential"
    event: Event = {
        "request": {"headers": {"referer": f"https://necturn.com/w/hotel-astana/b/{token}"}}
    }

    headers = add_context_tags(event, {})["request"]["headers"]

    assert isinstance(headers, dict)
    assert headers["referer"] == "https://necturn.com/w/hotel-astana/b/***"


def test_bind_link_token_reaches_sentry_nowhere_in_the_event(
    captured_sentry_events: list[Event], sentry_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Проверка ПОЛНОТЫ, а не отдельного поля: токен не должен встречаться нигде
    в событии. Путь запроса уезжает в Sentry не только как `request.url` —
    ещё заголовком `referer` и локальными переменными фреймов стектрейса
    (ревью PR #155).

    Падение на GET — это момент, когда токен заведомо ЖИВОЙ: страница согласия
    открыта, кнопка не нажата, потребляет ссылку только POST (spec 0033 §6).
    """

    async def exploding_resolve_tenant(tenant_slug: str) -> uuid.UUID:
        del tenant_slug
        raise RuntimeError("db blip while the guest reads the consent screen")

    monkeypatch.setattr("hospitality.channels.web.service.resolve_tenant", exploding_resolve_tenant)
    client = TestClient(sentry_app, raise_server_exceptions=False)
    # Токен случайный, а не литерал: TestClient зовёт приложение из этого же
    # стека, а Sentry прикладывает к фрейму строки исходника — литерал совпал бы
    # сам с собой и проверка перестала бы что-либо значить.
    token = uuid.uuid4().hex
    path = f"/w/hotel-astana/b/{token}"

    response = client.get(path, headers={"Referer": f"https://necturn.com{path}"})
    sentry_sdk.get_client().flush()

    assert response.status_code == 500
    assert captured_sentry_events, "необработанная ошибка обязана породить событие"
    for event in captured_sentry_events:
        assert token not in json.dumps(event, default=str), "токен утёк в событие Sentry"


def test_bind_link_token_reaches_sentry_nowhere_when_failure_precedes_routing(
    captured_sentry_events: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тот же токен, но падение ДО роутинга — в middleware.

    Тест выше роняет запрос внутри обработчика, где маршрут уже совпал и имя
    транзакции — шаблон `/w/{tenant_slug}/b/{token}`. Пока маршрута в ASGI-scope
    нет, интеграция берёт имя транзакции из СЫРОГО URL
    (`_get_transaction_name_and_source`, `transaction_info.source = "url"`) —
    четвёртое поле, которым живой токен уезжает в трекер (ревью PR #155).
    """

    async def exploding_resolver(scope: Scope) -> uuid.UUID | None:
        del scope
        raise RuntimeError("resolver blip before the router matched the route")

    # Патч ДО сборки приложения: цепочку резолверов composition root читает
    # один раз, в create_app.
    monkeypatch.setattr("hospitality.app.resolve_tenant_from_service_token", exploding_resolver)
    client = TestClient(create_app(), raise_server_exceptions=False)
    token = uuid.uuid4().hex  # случайный — по той же причине, что и в тесте выше
    path = f"/w/hotel-astana/b/{token}"

    response = client.get(path, headers={"Referer": f"https://necturn.com{path}"})
    sentry_sdk.get_client().flush()

    assert response.status_code == 500
    assert captured_sentry_events, "необработанная ошибка обязана породить событие"
    for event in captured_sentry_events:
        assert token not in json.dumps(event, default=str), "токен утёк в событие Sentry"
