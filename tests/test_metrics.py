"""Task 0018: метрики Prometheus-формата (FOUNDATION §10.7).

RED-метрики пишет CorrelationIdMiddleware, глубину outbox `/metrics` считает
в момент scrape. Счётчики глобальны на процесс (реестр prometheus_client),
поэтому тесты сравнивают приращения, а не абсолютные значения.
"""

from __future__ import annotations

import math
import uuid

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from hospitality.app import create_app
from hospitality.platform.events import CanaryCreated
from hospitality.platform.models import Tenant
from hospitality.shared import metrics
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.events import publish
from hospitality.shared.tenancy import tenant_context

REQUESTS_TOTAL = "http_requests_total"
DURATION_COUNT = "http_request_duration_seconds_count"


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return value if value is not None else 0.0


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_metrics_endpoint_serves_prometheus_text_with_red_labels(client: TestClient) -> None:
    labels = {"method": "GET", "route": "/health/live", "status": "2xx"}
    duration_labels = {"method": "GET", "route": "/health/live"}
    requests_before = _sample(REQUESTS_TOTAL, labels)
    duration_before = _sample(DURATION_COUNT, duration_labels)

    assert client.get("/health/live").status_code == 200
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert REQUESTS_TOTAL in response.text
    assert _sample(REQUESTS_TOTAL, labels) == requests_before + 1
    assert _sample(DURATION_COUNT, duration_labels) == duration_before + 1


def test_route_label_is_template_not_raw_path(client: TestClient) -> None:
    """Лейбл route — шаблон маршрута: сырой путь (UUID) взорвал бы кардинальность."""
    request_id = uuid.uuid4()
    template_labels = {
        "method": "GET",
        "route": "/api/v1/requests/{request_id}",
        "status": "4xx",  # без сервисного токена канонический эндпоинт отдаёт 401
    }
    before = _sample(REQUESTS_TOTAL, template_labels)

    assert client.get(f"/api/v1/requests/{request_id}").status_code == 401

    assert _sample(REQUESTS_TOTAL, template_labels) == before + 1
    raw_path_labels = dict(template_labels, route=f"/api/v1/requests/{request_id}")
    assert REGISTRY.get_sample_value(REQUESTS_TOTAL, raw_path_labels) is None


def test_unmatched_requests_collapse_to_single_label(client: TestClient) -> None:
    """404 сканеров по случайным путям не плодят временные ряды (unmatched)."""
    labels = {"method": "GET", "route": metrics.UNMATCHED_ROUTE, "status": "4xx"}
    before = _sample(REQUESTS_TOTAL, labels)

    for _ in range(2):
        assert client.get(f"/no/such/route-{uuid.uuid4().hex}").status_code == 404

    assert _sample(REQUESTS_TOTAL, labels) == before + 2


async def test_outbox_depth_reflects_pending_events(canonical_database: None) -> None:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-metrics", name="Hotel Metrics")
        session.add(tenant)
        await session.flush()
        tenant_id = tenant.id

    with tenant_context(tenant_id):
        async with session_scope() as session:
            await publish(session, CanaryCreated(canary_id=uuid.uuid4(), note="metrics"))

    await metrics._refresh_outbox_depth()

    assert _sample("outbox_pending_events") >= 1


async def test_outbox_depth_is_nan_when_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Недоступная БД не роняет /metrics: алертер обязан продолжать читать 5xx."""

    def broken_session_scope() -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(metrics, "platform_session_scope", broken_session_scope)

    await metrics._refresh_outbox_depth()

    value = REGISTRY.get_sample_value("outbox_pending_events")
    assert value is not None and math.isnan(value)


def test_metrics_endpoint_survives_database_outage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_session_scope() -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(metrics, "platform_session_scope", broken_session_scope)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert REQUESTS_TOTAL in response.text
