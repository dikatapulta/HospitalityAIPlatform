"""HTTP API заявок (Task 0013): happy path, ошибки с кодами каталога,
аутентификация сервисным токеном, изоляция тенантов через API, пагинация,
OpenAPI.

Канон HTTP-теста с БД: `httpx.AsyncClient` поверх ASGI-приложения из
настоящего composition root (`create_app`) — всё в одном event loop с
async-фикстурами БД (sync `TestClient` гоняет каждый запрос в собственном
loop'е, и пул asyncpg ломается о смену loop'а). Токен и slug тенанта
задаются переменными окружения до сборки приложения.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.modules.requests.api import ServiceRequestCreate, create_request
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.platform.auth import ERR_UNAUTHENTICATED
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context

TEST_SERVICE_TOKEN = "test-service-token"  # noqa: S105 — тестовое значение, не секрет
AUTH = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}


@pytest.fixture
def _reset_settings_cache() -> Iterator[None]:
    """Настройки читаются лениво и кэшируются — тестам с подменой env нужен сброс."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def api_client(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    _reset_settings_cache: None,
) -> AsyncIterator[AsyncClient]:
    """Клиент API, сервисный токен привязан к тенанту «Hotel A» (`hotel-a`)."""
    monkeypatch.setenv("SERVICE_TOKEN", TEST_SERVICE_TOKEN)
    monkeypatch.setenv("SERVICE_TOKEN_TENANT_SLUG", "hotel-a")
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_create_request_via_api_and_read_back(
    api_client: AsyncClient, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """DoD задачи: заявка создаётся HTTP-вызовом и видна в списке."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    created = await api_client.post(
        "/api/v1/requests",
        json={"category_id": str(category.id), "summary": "Clean room 204", "room_number": "204"},
        headers=AUTH,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "new"
    assert body["category_id"] == str(category.id)

    fetched = await api_client.get(f"/api/v1/requests/{body['id']}", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.json() == body

    listed = await api_client.get("/api/v1/requests", headers=AUTH)
    assert listed.status_code == 200
    page = listed.json()
    assert page["total"] == 1
    assert [item["id"] for item in page["items"]] == [body["id"]]


async def test_create_request_with_unknown_category_returns_catalog_error(
    api_client: AsyncClient,
) -> None:
    response = await api_client.post(
        "/api/v1/requests",
        json={"category_id": str(uuid.uuid4()), "summary": "no such category"},
        headers=AUTH,
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "ERR-REQUESTS-001"
    # Канонический конверт ошибки: correlation id есть и в теле, и в заголовке.
    assert error["correlation_id"] == response.headers["X-Correlation-ID"]


async def test_status_transitions_via_api(
    api_client: AsyncClient, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    created = await api_client.post(
        "/api/v1/requests",
        json={"category_id": str(category.id), "summary": "Fix the shower"},
        headers=AUTH,
    )
    request_id = created.json()["id"]

    assigned = await api_client.post(
        f"/api/v1/requests/{request_id}/status", json={"status": "assigned"}, headers=AUTH
    )
    assert assigned.status_code == 200
    assert assigned.json()["status"] == "assigned"

    # Недопустимый переход (assigned → done, минуя in_progress) — 409 с кодом.
    invalid = await api_client.post(
        f"/api/v1/requests/{request_id}/status", json={"status": "done"}, headers=AUTH
    )
    assert invalid.status_code == 409
    assert invalid.json()["error"]["code"] == "ERR-REQUESTS-003"

    # Неизвестное значение статуса отсекает валидация схемы, не бизнес-логика.
    unknown_status = await api_client.post(
        f"/api/v1/requests/{request_id}/status", json={"status": "teleported"}, headers=AUTH
    )
    assert unknown_status.status_code == 422
    assert unknown_status.json()["error"]["code"] == "ERR-PLATFORM-002"

    missing = await api_client.post(
        f"/api/v1/requests/{uuid.uuid4()}/status", json={"status": "assigned"}, headers=AUTH
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ERR-REQUESTS-002"


async def test_list_requests_paginates_newest_first(
    api_client: AsyncClient, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    created_ids = []
    for number in range(3):
        response = await api_client.post(
            "/api/v1/requests",
            json={"category_id": str(category.id), "summary": f"request {number}"},
            headers=AUTH,
        )
        created_ids.append(response.json()["id"])

    first_page = (await api_client.get("/api/v1/requests?limit=2", headers=AUTH)).json()
    assert first_page["total"] == 3
    assert first_page["limit"] == 2
    assert [item["id"] for item in first_page["items"]] == [created_ids[2], created_ids[1]]

    second_page = (await api_client.get("/api/v1/requests?limit=2&offset=2", headers=AUTH)).json()
    assert [item["id"] for item in second_page["items"]] == [created_ids[0]]

    # Границы среза защищает валидация HTTP-слоя, до сервиса не доходит.
    out_of_bounds = await api_client.get("/api/v1/requests?limit=0", headers=AUTH)
    assert out_of_bounds.status_code == 422


async def test_list_categories_returns_tenant_categories_sorted_by_key(
    api_client: AsyncClient, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tenant_a, _ = two_tenants
    await make_category(tenant_a, key="housekeeping", name="Уборка")
    await make_category(tenant_a, key="engineering", name="Инженерия")

    response = await api_client.get("/api/v1/requests/categories", headers=AUTH)

    assert response.status_code == 200
    assert [category["key"] for category in response.json()] == ["engineering", "housekeeping"]


async def test_api_does_not_see_other_tenant_data(
    api_client: AsyncClient, two_tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Изоляция тенантов через API (обязательный тест задачи): токен привязан
    к Hotel A — данные Hotel B для него не существуют (404), а не запрещены (403)."""
    _, tenant_b = two_tenants
    foreign_category = await make_category(tenant_b, key="spa", name="SPA")
    with tenant_context(tenant_b):
        foreign_request = await create_request(
            ServiceRequestCreate(category_id=foreign_category.id, summary="Hotel B secret")
        )

    listed = (await api_client.get("/api/v1/requests", headers=AUTH)).json()
    assert listed["total"] == 0

    categories = (await api_client.get("/api/v1/requests/categories", headers=AUTH)).json()
    assert categories == []

    fetched = await api_client.get(f"/api/v1/requests/{foreign_request.id}", headers=AUTH)
    assert fetched.status_code == 404
    assert fetched.json()["error"]["code"] == "ERR-REQUESTS-002"

    changed = await api_client.post(
        f"/api/v1/requests/{foreign_request.id}/status", json={"status": "assigned"}, headers=AUTH
    )
    assert changed.status_code == 404


# ---------------------------------------------------------------------------
# Аутентификация: негативные сценарии не требуют БД — резолвер отбрасывает
# запрос до обращения к реестру тенантов.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},  # без токена
        {"Authorization": f"Basic {TEST_SERVICE_TOKEN}"},  # не Bearer-схема
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Bearer"},  # схема без токена
    ],
)
async def test_request_without_valid_token_is_unauthorized(
    monkeypatch: pytest.MonkeyPatch, _reset_settings_cache: None, headers: dict[str, str]
) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", TEST_SERVICE_TOKEN)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/requests", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    error = response.json()["error"]
    assert error["code"] == ERR_UNAUTHENTICATED
    assert error["correlation_id"]


async def test_valid_token_with_missing_tenant_is_unauthorized(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    _reset_settings_cache: None,
) -> None:
    """Токен верный, но slug из конфигурации не существует в реестре — ошибка
    окружения: клиенту тот же 401, диагноз — по логу `service_token_tenant_missing`."""
    monkeypatch.setenv("SERVICE_TOKEN", TEST_SERVICE_TOKEN)
    monkeypatch.setenv("SERVICE_TOKEN_TENANT_SLUG", "ghost-hotel")
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/requests", headers=AUTH)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ERR_UNAUTHENTICATED


async def test_openapi_documents_requests_api(_reset_settings_cache: None) -> None:
    """DoD: OpenAPI-схема корректна — пути, security-схема и конверт ошибок на месте."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    paths = schema["paths"]
    assert "/api/v1/requests" in paths
    assert "/api/v1/requests/{request_id}/status" in paths
    assert "ServiceToken" in schema["components"]["securitySchemes"]
    create_operation = paths["/api/v1/requests"]["post"]
    assert {"ServiceToken": []} in create_operation["security"]
    assert "401" in create_operation["responses"]
    assert "404" in create_operation["responses"]
