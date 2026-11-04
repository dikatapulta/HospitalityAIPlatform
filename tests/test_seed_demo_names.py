"""Имена служб демо-отеля языком отеля (#833).

Сид с нуля даёт новые имена; на staging уже созданные службы с прежним именем
по умолчанию переименовываются, ручное имя остаётся, повтор ничего не меняет,
вне staging шаг не работает; бот по-прежнему находит службу по ключу.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai.tools import create_service_request
from hospitality.ai.tools.base import ToolTurnContext
from hospitality.modules.requests.api import (
    RequestCategoryCreate,
    create_category,
    list_categories,
)
from hospitality.platform.seed import seed_demo_tenant
from hospitality.shared.tenancy import tenant_context
from hospitality.tools import seed
from hospitality.tools.demo_showroom import DEMO_SHOWROOM_CATEGORIES
from hospitality.tools.seed import DEMO_CATEGORIES, DEMO_CATEGORY_FORMER_NAMES, seed_demo_data

pytestmark = pytest.mark.usefixtures("canonical_database")

NEW_NAMES = {
    category.key: category.name for category in (*DEMO_CATEGORIES, *DEMO_SHOWROOM_CATEGORIES)
}
HAND_NAME = "IT и связь"  # менеджер поменял имя сам — сид его не трогает


async def _categories(tenant_id: uuid.UUID) -> dict[str, tuple[uuid.UUID, str]]:
    with tenant_context(tenant_id):
        return {c.key: (c.id, c.name) for c in await list_categories()}


async def _staging_before_833() -> uuid.UUID:
    """Демо-отель staging до #833: службы с прежними именами, одна — с ручным."""
    tenant_id = await seed_demo_tenant()
    former = {**DEMO_CATEGORY_FORMER_NAMES, "it-support": HAND_NAME}
    with tenant_context(tenant_id):
        for key, name in former.items():
            await create_category(RequestCategoryCreate(key=key, name=name))
    return tenant_id


def _names(categories: dict[str, tuple[uuid.UUID, str]]) -> dict[str, str]:
    return {key: name for key, (_, name) in categories.items()}


async def test_seed_from_scratch_gives_hotel_names() -> None:
    await seed_demo_data()
    tenant_id = await seed_demo_tenant()

    names = _names(await _categories(tenant_id))
    assert names == NEW_NAMES
    assert names["housekeeping"] == "Хаускипинг"
    assert names["banquet-sales"] == "Отдел продаж банкетных залов"


async def test_staging_renames_former_names_once_and_keeps_hand_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = await _staging_before_833()
    before = await _categories(tenant_id)
    monkeypatch.setattr(seed, "staging_refusals", lambda *, confirmed: [])

    await seed_demo_data()
    after = await _categories(tenant_id)
    await seed_demo_data()

    assert await _categories(tenant_id) == after  # повтор ничего не меняет
    assert _names(after) == {**NEW_NAMES, "it-support": HAND_NAME}
    # Ключи и id прежние: заявки, сроки и маршруты служб остаются на месте.
    assert {key: before[key][0] for key in before} == {key: after[key][0] for key in before}

    # Бот выбирает службу ключом — заявка «fnb» ложится в ту же службу, что до #833.
    with tenant_context(tenant_id):
        created = await create_service_request.execute(
            {"category_key": "fnb", "summary": "Чай в номер", "room_number": "1507"},
            ToolTurnContext(),
        )
    assert created.category_id == before["fnb"][0]


async def test_rename_skipped_outside_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = await _staging_before_833()
    monkeypatch.setattr(seed, "staging_refusals", lambda *, confirmed: ["не staging"])

    await seed_demo_data()

    names = _names(await _categories(tenant_id))
    assert names["fnb"] == "Ресторан и room service"
    assert names["reservations"] == "Бронирование"
