"""Профиль запуска (#1053 п. 3): что отвечает каждая операция копии отеля.

Выключенный раздел остаётся частью программы отеля: его маршруты подключены без условий,
закрывает их проверка выключателя внутри операции. Этот тест собирает приложение с профилем
копии (`ops/deploy/pilot-copy-profile.toml`) и спрашивает каждую операцию дважды — анонимом
из интернета и «своим»: менеджером отеля (`/staff/…`), гостем с кодом заселения (`/g/…`,
`/w/…`), сервисным токеном (`/api/v1/…`); у остальных «свой» — тот же аноним. Ответы
сверяются с таблицей `tests/launch_profile_operations.py`. Красный, если:

- у операции нет строки — новая операция обязана заявить, что она отвечает на копии;
- строка есть, операции нет — строку удалённой операции убрать;
- ответ не тот — например, «тёмная» операция выключенного раздела (404) стала отвечать.

Запрос — без тела, у POST — пустой JSON; номера в пути — заглушки, которых в базе нет.
Поэтому 404 у операции с номером в пути — не обязательно выключатель, а 422 у POST — отказ
разбора тела раньше выключателя: таблица — снимок поверхности копии, а не доказательство
каждого выключателя (их проверяют тесты разделов). Модели — Fake-адаптеры: вызов модели
без сценария роняет операцию в 500, и строка это покажет. Готовность (`/health/ready`) —
как у здорового контура: Redis в CI нет. Ответы, зависящие от секретов контура (вебхук
Telegram, выгрузка Opera), — как в тестовом окружении.

Правка таблицы: прогон печатает расхождения и готовые строки — вставить их, если новый ответ
и есть намерение. Включили раздел на копии — строка профиля, затем таблица, тем же PR.
F811 отключён на файл: параметр-фикстура обязан называться как реимпортированная фикстура.
"""

# ruff: noqa: F811

from __future__ import annotations

import re
import tomllib
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from httpx import ASGITransport, AsyncClient

from hospitality.ai.gateway.api import ScriptedLlmProvider
from hospitality.ai.mentor.tests.conftest import providers
from hospitality.app import create_app
from hospitality.channels.telegram.router import get_orchestrator_provider
from hospitality.channels.web.router import get_web_llm_provider
from hospitality.modules.guests.api import StayCheckIn, check_in
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.ports.pms import pms_or_none, set_pms
from hospitality.shared.staging_features import STAGING_SECTION_MODELS
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal.memos_format_api import get_memo_format_provider
from hospitality.staff_portal.mentor_api import get_mentor_providers
from hospitality.staff_portal.tests.conftest import (  # noqa: F401  (фикстуры для pytest)
    HOTEL_SLUG,
    PortalHotel,
    _fake_rate_limit,
    portal_hotel,
    submit_login,
)
from tests.launch_profile_operations import OPERATIONS

PROFILE_PATH = Path(__file__).resolve().parents[1] / "ops" / "deploy" / "pilot-copy-profile.toml"
BASE_URL = "https://test"
SAME_ORIGIN = {"origin": BASE_URL}
ROOM = "101"
PLACEHOLDER_ID = str(uuid.UUID(int=1))
SERVICE_TOKEN = "launch-profile-service-token"  # noqa: S105 — тестовое значение, не секрет
DUMMY_PROVIDER_KEY = "launch-profile-key"
_PATH_PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")


@dataclass(frozen=True)
class Profile:
    """Строки `.env` копии и флаги её отеля — как в файле профиля."""

    env: dict[str, str]
    tenant: dict[str, Any]


def load_profile() -> Profile:
    data = tomllib.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    return Profile(env=dict(data.get("env", {})), tenant=dict(data.get("tenant", {})))


def profile_environment(profile: Profile) -> dict[str, str]:
    """Окружение процесса копии: умолчание каждому выключателю и модели раздела, затем
    строки профиля. Ключ провайдера модели — заглушка: секретов в профиле нет, а модели
    в тесте — Fake-адаптеры."""
    env: dict[str, str] = {}
    for name in Settings.model_fields:
        if name.startswith("enable_") or name == "staging_all_features":
            env[name.upper()] = "false"
    for name in STAGING_SECTION_MODELS:
        env[name.upper()] = ""
    env.update(profile.env)
    for name, (_, provider) in STAGING_SECTION_MODELS.items():
        if env[name.upper()]:
            env[f"{provider.upper()}_API_KEY"] = DUMMY_PROVIDER_KEY
    env["SERVICE_TOKEN"] = SERVICE_TOKEN
    env["SERVICE_TOKEN_TENANT_SLUG"] = HOTEL_SLUG
    return env


@dataclass(frozen=True)
class Operation:
    """Метод и шаблон пути операции; `url` — путь с заглушками вместо параметров."""

    method: str
    path: str
    url: str

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


def app_operations(app: FastAPI) -> list[Operation]:
    """Все операции приложения; выход из кабинета — последним, иначе менеджер вышел бы
    раньше, чем его спросили об остальном."""
    operations: list[Operation] = []
    for route in iter_route_contexts(app.routes):
        path = route.path
        assert path is not None, route  # у приложения только маршруты операций, без mount
        types = {param.name: param.field_info.annotation for param in route.dependant.path_params}
        url = _concrete_url(path, types)
        operations.extend(Operation(method, path, url) for method in route.methods or ())
    return sorted(operations, key=lambda op: (op.path.endswith("/logout"), op.path, op.method))


def _concrete_url(path: str, types: dict[str, Any]) -> str:
    return _PATH_PARAM.sub(lambda match: _placeholder(match.group(1), types), path)


def _placeholder(name: str, types: dict[str, Any]) -> str:
    annotation = types.get(name)
    if name == "tenant_slug":
        return HOTEL_SLUG
    if name == "room_number":
        return ROOM
    if annotation is uuid.UUID:
        return PLACEHOLDER_ID
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return str(next(iter(annotation)).value)
    return "x"


@dataclass
class CopyClients:
    """Приложение копии и три браузера: аноним без cookie, менеджер отеля, гость с кодом."""

    app: FastAPI
    anonymous: AsyncClient
    manager: AsyncClient
    guest: AsyncClient


async def _healthy_redis(url: str) -> bool:
    del url
    return True


@pytest.fixture
def copy_environment() -> Iterator[Profile]:
    """Окружение профиля копии своим `MonkeyPatch.context()` (канон `fake_file_storage`):
    общий `monkeypatch` откатил бы его в чужом порядке. Порт PMS, который положит
    `create_app()` профиля, возвращается прежним."""
    profile = load_profile()
    previous_pms = pms_or_none()
    with pytest.MonkeyPatch.context() as patch:
        for name, value in profile_environment(profile).items():
            patch.setenv(name, value)
        patch.setattr("hospitality.shared.health.check_redis", _healthy_redis)
        get_settings.cache_clear()
        try:
            yield profile
        finally:
            if previous_pms is not None:
                set_pms(previous_pms)
            get_settings.cache_clear()
    get_settings.cache_clear()


def _copy_app() -> FastAPI:
    """Приложение профиля с Fake-моделями: платных и сетевых вызовов нет."""
    app = create_app()
    app.dependency_overrides[get_mentor_providers] = lambda: providers()
    for dependency in (get_web_llm_provider, get_orchestrator_provider, get_memo_format_provider):
        app.dependency_overrides[dependency] = lambda: ScriptedLlmProvider([])
    return app


@pytest.fixture
async def copy_clients(
    portal_hotel: PortalHotel, copy_environment: Profile
) -> AsyncIterator[CopyClients]:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            portal_hotel.tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                **copy_environment.tenant,
            ),
        )
    with tenant_context(portal_hotel.tenant_id):
        stay = await check_in(
            StayCheckIn(room_number=ROOM, check_out_at=utc_now() + timedelta(days=1))
        )
    app = _copy_app()
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url=BASE_URL) as anonymous,
        AsyncClient(transport=transport, base_url=BASE_URL) as manager,
        AsyncClient(transport=transport, base_url=BASE_URL) as guest,
    ):
        assert (await submit_login(manager, portal_hotel.login)).status_code == 303
        bound = await guest.post(
            f"/g/{HOTEL_SLUG}/{ROOM}/session", json={"code": stay.access_code}, headers=SAME_ORIGIN
        )
        assert bound.status_code == 200, bound.text
        yield CopyClients(app, anonymous, manager, guest)


async def _answer(client: AsyncClient, op: Operation, headers: dict[str, str]) -> int:
    if op.method == "GET":
        return (await client.get(op.url, headers=headers)).status_code
    response = await client.request(op.method, op.url, json={}, headers=SAME_ORIGIN | headers)
    return response.status_code


async def _anonymous_answer(clients: CopyClients, op: Operation, headers: dict[str, str]) -> int:
    clients.anonymous.cookies.clear()  # ответ мог поставить cookie — аноним остаётся анонимом
    return await _answer(clients.anonymous, op, headers)


async def _own_answer(clients: CopyClients, op: Operation) -> int:
    if op.path.startswith("/staff/"):
        return await _answer(clients.manager, op, {})
    if op.path.startswith(("/g/", "/w/")):
        return await _answer(clients.guest, op, {})
    if op.path.startswith("/api/v1/"):
        return await _anonymous_answer(clients, op, {"authorization": f"Bearer {SERVICE_TOKEN}"})
    return await _anonymous_answer(clients, op, {})


def test_profile_names_real_switches() -> None:
    """Опечатка в профиле молча оставила бы раздел выключенным: каждый ключ — настоящее
    поле настроек или конфига отеля, а выключатель «всё на staging» в профиле запрещён."""
    profile = load_profile()
    assert {name.lower() for name in profile.env} <= set(Settings.model_fields)
    assert set(profile.tenant) <= set(TenantConfig.model_fields)
    assert "STAGING_ALL_FEATURES" not in profile.env  # в .env копии не ставится никогда


async def test_every_operation_answers_as_the_table_says(copy_clients: CopyClients) -> None:
    actual: dict[str, tuple[int, int]] = {}
    for op in app_operations(copy_clients.app):
        anonymous = await _anonymous_answer(copy_clients, op, {})
        actual[op.key] = (anonymous, await _own_answer(copy_clients, op))
    differences = [
        f"{key}: в таблице {OPERATIONS.get(key)}, ответ {actual.get(key)}"
        for key in sorted(set(actual) | set(OPERATIONS))
        if actual.get(key) != OPERATIONS.get(key)
    ]
    rows = [
        f'    "{key}": {answer},' for key, answer in actual.items() if answer != OPERATIONS.get(key)
    ]
    assert not differences, "\n".join(
        ["Ответы копии разошлись с tests/launch_profile_operations.py:", *differences, "", *rows]
    )
