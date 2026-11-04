"""`rename_category` (#833): имя меняется, только если оно прежнее; ключ и id — те же."""

from __future__ import annotations

import uuid

from hospitality.modules.requests.api import list_categories, rename_category
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.tenancy import tenant_context


async def _names(tenant_id: uuid.UUID) -> dict[str, str]:
    with tenant_context(tenant_id):
        return {category.key: category.name for category in await list_categories()}


async def test_rename_only_from_expected_name_and_only_own_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    original = await make_category(tenant_a, key="fnb", name="Ресторан и room service")
    await make_category(tenant_a, key="it-support", name="IT и связь")
    await make_category(tenant_b, key="fnb", name="Ресторан и room service")

    with tenant_context(tenant_a):
        assert await rename_category("fnb", expected_name="Ресторан и room service", new_name="F&B")
        # Повтор — уже не прежнее имя: ничего не меняется.
        assert not await rename_category(
            "fnb", expected_name="Ресторан и room service", new_name="F&B"
        )
        # Имя, поменянное руками, не трогается; службы нет — тоже False.
        assert not await rename_category("it-support", expected_name="Wi-Fi и ТВ", new_name="IT")
        assert not await rename_category("laundry", expected_name="Прачечная", new_name="X")
        renamed = next(c for c in await list_categories() if c.key == "fnb")

    assert renamed.id == original.id
    assert await _names(tenant_a) == {"fnb": "F&B", "it-support": "IT и связь"}
    # Тот же ключ у соседа — не его служба (RLS, P-4).
    assert await _names(tenant_b) == {"fnb": "Ресторан и room service"}
