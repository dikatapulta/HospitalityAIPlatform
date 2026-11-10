"""#487, ADR-006 §7 п. 7: описание API — только в dev.

Снаружи `/docs`, `/redoc` и `/openapi.json` — карта всех маршрутов, включая
вход суперадмина; staging получает `SENTRY_ENVIRONMENT=staging` из compose-файла,
гостевая копия — `pilot` из своего `.env` (#389), локальная разработка — `dev`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from hospitality.app import create_app
from hospitality.shared.config import get_settings

API_DESCRIPTION_PATHS = ("/docs", "/redoc", "/openapi.json")


@pytest.fixture(autouse=True)
def _settings_from_env() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize("environment", ["staging", "pilot"])
@pytest.mark.parametrize("path", API_DESCRIPTION_PATHS)
def test_api_description_is_hidden_outside_dev(
    path: str, environment: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SENTRY_ENVIRONMENT", environment)

    assert TestClient(create_app()).get(path).status_code == 404


@pytest.mark.parametrize("path", API_DESCRIPTION_PATHS)
def test_api_description_is_served_in_dev(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "dev")

    assert TestClient(create_app()).get(path).status_code == 200
