"""Фикстуры тестов помощника витрины (spec 0069 §7 п. 4): отель и включённое назначение
`showroom`. Инфраструктура реимпортируется из `tests/conftest.py` (канон).

F811 отключён на файл: фикстура-параметр обязана называться как реимпортированная
фикстура — так pytest связывает их.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from hospitality.ai.evals.showroom_navigate import EVAL_SCENE
from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from tests.conftest import (  # noqa: F401  (реимпорт общих фикстур для pytest)
    _clean_log_context,
    _isolated_event_subscribers,
    canonical_database,
    migrated_database_name,
)

SHOWROOM_MODEL = "claude-sonnet-5"


@pytest.fixture
async def hotel(canonical_database: None) -> uuid.UUID:
    async with platform_session_scope() as session:
        tenant = Tenant(slug="showroom-hotel", name="Showroom Hotel")
        session.add(tenant)
        await session.flush()
        return tenant.id


@pytest.fixture
def showroom_model() -> Iterator[None]:
    """Назначение `showroom` включено, одна попытка провайдера — без пауз ретрая."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_MODEL_SHOWROOM", SHOWROOM_MODEL)
        patch.setenv("LLM_MAX_ATTEMPTS", "1")
        get_settings.cache_clear()
        yield
    get_settings.cache_clear()


class FakeRooms:
    """`showroom.api.find_rooms` по `EVAL_SCENE` без БД: поиск — забота модуля (PR а)."""

    def __init__(self) -> None:
        self.queries: list[showroom_api.RoomQuery] = []

    async def __call__(self, query: showroom_api.RoomQuery) -> list[showroom_api.RoomMatch]:
        self.queries.append(query)
        matches = [
            showroom_api.RoomMatch(
                number=room.number,
                floor=floor.number,
                side=room.side,
                type_key=room.type_key,
                view_keys=room.view_keys,
            )
            for floor in sorted(EVAL_SCENE.floors, key=lambda item: -item.number)
            if floor.sellable
            and (query.floor_from is None or floor.number >= query.floor_from)
            and (query.floor_to is None or floor.number <= query.floor_to)
            for room in floor.rooms
            if (query.side is None or room.side == query.side)
            and (query.view_key is None or query.view_key in room.view_keys)
            and (query.type_key is None or room.type_key == query.type_key)
        ]
        return matches[: query.count]


@pytest.fixture
def fake_rooms() -> Iterator[FakeRooms]:
    """Свой `MonkeyPatch.context()`, а не общий `monkeypatch`: общий, взятый раньше
    `canonical_database`, откатил бы `POSTGRES_DB` позже удаления временной БД."""
    rooms = FakeRooms()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(showroom_api, "find_rooms", rooms)
        yield rooms
