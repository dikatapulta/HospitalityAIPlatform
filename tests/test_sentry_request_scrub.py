"""#972: адрес клиента, строка запроса и секрет вебхука не доходят до Sentry.

Находки Л-1, Л-2, Л-5 аудита 10.11: `send_default_pii=False` режет только
заголовки из списка SDK (`Authorization`, `X-Forwarded-For` …), а
`CF-Connecting-IP` края, `X-Telegram-Bot-Api-Secret-Token`, `query_string`,
`Referer` и ключ лимита по адресу в крошке Redis уезжали в событие.

Маркеры — латиницей и случайные: `json.dumps` экранирует кириллицу, а Sentry
прикладывает к фреймам строки исходника — литерал совпал бы сам с собой.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis.asyncio as redis
import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sentry_sdk.types import Breadcrumb, Event

from hospitality.app import create_app
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.sentry import add_context_tags, init_sentry, strip_breadcrumb
from tests.test_sentry import CapturingTransport

# Порт, на котором Redis заведомо нет: крошка пишется до попытки соединения.
_DEAD_REDIS = "redis://127.0.0.1:1/0"


def _client_ip() -> str:
    """Адрес из диапазона документации (RFC 5737), последний октет случайный."""
    return f"198.51.100.{secrets.randbelow(200) + 20}"


def _dumps(events: list[Event]) -> list[str]:
    return [json.dumps(event, default=str) for event in events]


@pytest.fixture
def captured_sentry_events() -> Iterator[list[Event]]:
    transport = CapturingTransport()
    init_sentry(
        Settings(sentry_dsn="https://public@sentry.invalid/1", sentry_environment="test"),
        transport=transport,
    )
    yield transport.events
    sentry_sdk.get_client().flush()
    sentry_sdk.get_global_scope().set_client(None)


@pytest.fixture
def scrub_app() -> FastAPI:
    """Настоящий composition root и два маршрута: 500 и ERROR-лог внутри запроса.

    Оба перед сбоем тратят лимит по адресу из заголовка края, как это делают
    вход в кабинет, ссылка гостя и витрина (`shared/clientip.py`), — на мёртвом
    Redis: так в событие попадает крошка с ключом, а лимит открывается (fail-open).
    """
    app = create_app()
    logger = get_logger(module="tests.test_sentry_request_scrub")

    async def spend_ip_limit(request: Request) -> None:
        client = redis.from_url(_DEAD_REDIS, socket_connect_timeout=0.2)
        try:
            await consume_rate_limit(
                "sentry_scrub_ip",
                request.headers["cf-connecting-ip"],
                limit=5,
                window_seconds=60,
                client=client,
            )
        finally:
            await client.aclose()

    @app.get("/sentry-scrub-boom")
    async def scrub_boom(request: Request) -> None:
        await spend_ip_limit(request)
        raise RuntimeError("sentry scrub explosion")

    @app.get("/sentry-scrub-error-log")
    async def scrub_error_log(request: Request) -> dict[str, str]:
        await spend_ip_limit(request)
        logger.error("sentry_scrub_error_logged")
        return {"status": "ok"}

    return app


@pytest.mark.parametrize(
    ("path", "status", "diagnosis"),
    [
        ("/sentry-scrub-boom", 500, "sentry scrub explosion"),
        ("/sentry-scrub-error-log", 200, "sentry_scrub_error_logged"),
    ],
)
def test_client_address_and_query_reach_sentry_nowhere(
    captured_sentry_events: list[Event],
    scrub_app: FastAPI,
    path: str,
    status: int,
    diagnosis: str,
) -> None:
    ip = _client_ip()
    query_marker = f"guestsearch{uuid.uuid4().hex}"
    client = TestClient(scrub_app, raise_server_exceptions=False)

    response = client.get(
        f"{path}?q={query_marker}",
        headers={
            "CF-Connecting-IP": ip,
            "True-Client-IP": ip,
            "Referer": f"https://necturn.com/staff/requests?q={query_marker}",
            "User-Agent": "scrub-test-agent",
        },
    )
    sentry_sdk.get_client().flush()

    assert response.status_code == status
    dumps = _dumps(captured_sentry_events)
    assert any(diagnosis in dump for dump in dumps), "событие обязано родиться"
    events: list[dict[str, Any]] = [json.loads(dump) for dump in dumps]
    redis_crumbs = [
        crumb
        for event in events
        for crumb in event.get("breadcrumbs", {}).get("values", [])
        if crumb.get("category") == "redis"
    ]
    assert redis_crumbs, "проба: крошка Redis обязана быть в событии"
    for dump in dumps:
        assert ip not in dump, "адрес клиента утёк в событие Sentry"
        assert query_marker not in dump, "строка запроса утекла в событие Sentry"
    for event in events:
        headers = event["request"]["headers"]
        assert headers.get("user-agent") == "scrub-test-agent", "белый список режет лишнее"


def test_telegram_webhook_secret_reaches_sentry_nowhere(
    captured_sentry_events: list[Event], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Секрет — единственная аутентификация вебхука (Л-2): прочитавший событие
    слал бы поддельные обновления от имени чата службы."""
    webhook_secret = uuid.uuid4().hex
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", webhook_secret)
    get_settings.cache_clear()

    async def exploding_process_update(*args: object, **kwargs: object) -> None:
        raise RuntimeError("telegram update processing blew up")

    monkeypatch.setattr(
        "hospitality.channels.telegram.router.process_update", exploding_process_update
    )
    try:
        client = TestClient(create_app(), raise_server_exceptions=False)
        response = client.post(
            "/channels/telegram/webhook",
            json={"update_id": 1, "message": {"message_id": 7, "chat": {"id": 555}, "text": "hi"}},
            headers={"X-Telegram-Bot-Api-Secret-Token": webhook_secret},
        )
        sentry_sdk.get_client().flush()
    finally:
        get_settings.cache_clear()

    assert response.status_code == 500
    dumps = _dumps(captured_sentry_events)
    assert any("telegram update processing blew up" in dump for dump in dumps)
    for dump in dumps:
        assert webhook_secret not in dump, "секрет вебхука утёк в событие Sentry"


def test_before_send_keeps_only_allowed_headers_and_drops_query_string() -> None:
    event: Event = {
        "request": {
            "url": "https://necturn.com/staff/requests",
            "query_string": "q=room+305",
            "headers": {
                "host": "necturn.com",
                "content-type": "application/json",
                "content-length": "12",
                "user-agent": "Mozilla/5.0",
                "cf-connecting-ip": "198.51.100.7",
                "cf-ipcountry": "KZ",
                "true-client-ip": "198.51.100.7",
                "referer": "https://necturn.com/staff/requests?q=room+305",
                "x-telegram-bot-api-secret-token": "s3cr3t",
            },
        }
    }

    request = add_context_tags(event, {})["request"]

    assert "query_string" not in request
    assert request["headers"] == {
        "host": "necturn.com",
        "content-type": "application/json",
        "content-length": "12",
        "user-agent": "Mozilla/5.0",
    }


def test_redis_breadcrumb_keeps_only_the_command() -> None:
    crumb: Breadcrumb = {
        "type": "redis",
        "category": "redis",
        "message": "INCR 'ratelimit:staff_login_ip:600:198.51.100.7:2950000'",
        "data": {
            "redis.is_cluster": False,
            "redis.command": "INCR",
            "db.operation": "INCR",
            "redis.key": "ratelimit:staff_login_ip:600:198.51.100.7:2950000",
        },
    }

    stripped = strip_breadcrumb(crumb, {})

    assert "message" not in stripped
    assert stripped["data"] == {
        "redis.is_cluster": False,
        "redis.command": "INCR",
        "db.operation": "INCR",
    }
