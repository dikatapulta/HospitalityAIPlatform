"""Каталоги room service (spec 0038 §3, §4, §6 PR 3): порядок, скрытие и
«Закончилось», «не принимаем» без строки, закрытый каталог, изоляция тенантов;
перестановка при гонке (ревью PR #508, П-1 и П-2 — починено в PR 4)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import get_args

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from hospitality.modules.room_service.api import (
    ERR_ROOMSERVICE_CATALOG_DISABLED,
    ERR_ROOMSERVICE_NOT_FOUND,
    MAX_PRICE_MINOR,
    ItemWrite,
    MoveDirection,
    RoomServiceCatalog,
    SectionWrite,
    create_item,
    create_section,
    get_catalog,
    get_settings,
    move_item,
    move_section,
    rename_section,
    set_accepting,
    set_available,
    set_hidden,
    update_item,
)
from hospitality.modules.room_service.models import RoomServiceSection
from hospitality.modules.room_service.service import _renumber
from hospitality.modules.room_service.tests.conftest import configure_tenant, room_service_config
from hospitality.platform.config import LocalizedText, RoomServiceCatalogKey, RoomServiceConfig
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

Tenants = tuple[uuid.UUID, uuid.UUID]


def _names(en: str, ru: str | None = None) -> LocalizedText:
    return LocalizedText(en=en, ru=ru)


async def _section(catalog: RoomServiceCatalog, en: str) -> uuid.UUID:
    return (await create_section(catalog, SectionWrite(names=_names(en)))).id


async def _item(section_id: uuid.UUID, en: str, price_tenge: int = 1000) -> uuid.UUID:
    data = ItemWrite(section_id=section_id, names=_names(en), price_minor=price_tenge * 100)
    return (await create_item(data)).id


async def _staff_view(catalog: RoomServiceCatalog) -> list[tuple[str, list[str]]]:
    view = await get_catalog(catalog, include_hidden=True)
    return [
        (section.names.en, [item.names.en for item in section.items]) for section in view.sections
    ]


def test_catalog_values_match_config_keys() -> None:
    """Каталог модуля и ключ конфига — одни значения (kernel модуль не импортирует)."""
    assert {catalog.value for catalog in RoomServiceCatalog} == set(get_args(RoomServiceCatalogKey))


async def test_sections_and_items_keep_order_and_move(two_tenants: Tenants) -> None:
    with tenant_context(two_tenants[0]):
        breakfast = await _section(RoomServiceCatalog.RESTAURANT, "Breakfast")
        soups = await _section(RoomServiceCatalog.RESTAURANT, "Soups")
        drinks = await _section(RoomServiceCatalog.RESTAURANT, "Drinks")
        syrniki = await _item(breakfast, "Syrniki")
        await _item(breakfast, "Omelette")
        porridge = await _item(breakfast, "Porridge")
        await _section(RoomServiceCatalog.SHOP, "Toiletries")

        await move_section(drinks, MoveDirection.UP)
        await move_item(porridge, MoveDirection.UP)
        await move_item(syrniki, MoveDirection.UP)  # уже первый — без изменений
        await move_section(soups, MoveDirection.DOWN)  # уже последний — без изменений

        assert await _staff_view(RoomServiceCatalog.RESTAURANT) == [
            ("Breakfast", ["Syrniki", "Porridge", "Omelette"]),
            ("Drinks", []),
            ("Soups", []),
        ]
        # Каталоги не смешиваются: раздел магазина в меню ресторана не попал.
        assert await _staff_view(RoomServiceCatalog.SHOP) == [("Toiletries", [])]


async def test_move_renumbers_equal_positions(two_tenants: Tenants) -> None:
    """Гонка двух «Добавить раздел» даёт равные позиции; перестановка всё равно сдвигает."""
    with tenant_context(two_tenants[0]):
        first = await _section(RoomServiceCatalog.RESTAURANT, "First")
        second = await _section(RoomServiceCatalog.RESTAURANT, "Second")
        async with session_scope() as session:
            await session.execute(update(RoomServiceSection).values(position=0))

        await move_section(second, MoveDirection.UP)

        assert [name for name, _ in await _staff_view(RoomServiceCatalog.RESTAURANT)] == [
            "Second",
            "First",
        ]
        assert (await move_section(first, MoveDirection.UP)).position == 0


async def test_guest_view_hides_hidden_items_and_empty_sections(two_tenants: Tenants) -> None:
    with tenant_context(two_tenants[0]):
        mains = await _section(RoomServiceCatalog.RESTAURANT, "Mains")
        desserts = await _section(RoomServiceCatalog.RESTAURANT, "Desserts")
        await _section(RoomServiceCatalog.RESTAURANT, "Empty")
        plov = await _item(mains, "Plov")
        beshbarmak = await _item(mains, "Beshbarmak")
        cake = await _item(desserts, "Honey cake")

        sold_out = await set_available(beshbarmak, False)
        hidden = await set_hidden(cake, True)
        again = await set_hidden(cake, True)

        assert sold_out.is_available is False
        assert hidden.hidden_at is not None
        assert again.hidden_at == hidden.hidden_at  # повтор не сдвигает момент скрытия
        guest = await get_catalog(RoomServiceCatalog.RESTAURANT)
        # «Закончилось» гость видит, скрытое — нет; раздел без видимых товаров пропал.
        assert [
            (s.names.en, [(i.names.en, i.is_available) for i in s.items]) for s in guest.sections
        ] == [("Mains", [("Plov", True), ("Beshbarmak", False)])]
        assert [name for name, _ in await _staff_view(RoomServiceCatalog.RESTAURANT)] == [
            "Mains",
            "Desserts",
            "Empty",
        ]

        await set_hidden(cake, False)
        await set_available(beshbarmak, True)
        guest = await get_catalog(RoomServiceCatalog.RESTAURANT)
        assert [section.names.en for section in guest.sections] == ["Mains", "Desserts"]
        assert all(item.is_available for item in guest.sections[0].items)
        assert plov in {item.id for item in guest.sections[0].items}


async def test_no_settings_row_means_not_accepting(two_tenants: Tenants) -> None:
    with tenant_context(two_tenants[0]):
        settings = await get_settings(RoomServiceCatalog.SHOP)
        assert settings.accepting_orders is False
        assert (settings.category_key, settings.service_charge_percent) == ("supply", Decimal(0))

        assert (await set_accepting(RoomServiceCatalog.SHOP, True)).accepting_orders is True
        assert (await get_settings(RoomServiceCatalog.SHOP)).accepting_orders is True
        assert (await get_settings(RoomServiceCatalog.RESTAURANT)).accepting_orders is False

        await set_accepting(RoomServiceCatalog.SHOP, False)
        assert (await get_settings(RoomServiceCatalog.SHOP)).accepting_orders is False


async def test_update_item_saves_form_and_moves_to_end_of_new_section(
    two_tenants: Tenants,
) -> None:
    with tenant_context(two_tenants[0]):
        soups = await _section(RoomServiceCatalog.RESTAURANT, "Soups")
        mains = await _section(RoomServiceCatalog.RESTAURANT, "Mains")
        await _item(mains, "Plov")
        lagman = await _item(soups, "Lagman", price_tenge=3400)

        updated = await update_item(
            lagman,
            ItemWrite(
                section_id=mains,
                names=_names("Lagman noodles", ru="Лагман"),
                descriptions=LocalizedText(en="Beef, vegetables", kk="  "),
                price_minor=3600_00,
                is_available=False,
            ),
        )

        assert updated.section_id == mains
        assert updated.names.text_for("ru") == "Лагман"
        assert updated.names.text_for("kk") == "Lagman noodles"  # перевода нет — английский
        assert updated.descriptions is not None and updated.descriptions.kk is None
        assert (updated.price_minor, updated.is_available) == (360000, False)
        assert await _staff_view(RoomServiceCatalog.RESTAURANT) == [
            ("Soups", []),
            ("Mains", ["Plov", "Lagman noodles"]),
        ]


async def test_item_cannot_move_to_another_catalog(two_tenants: Tenants) -> None:
    with tenant_context(two_tenants[0]):
        mains = await _section(RoomServiceCatalog.RESTAURANT, "Mains")
        toiletries = await _section(RoomServiceCatalog.SHOP, "Toiletries")
        plov = await _item(mains, "Plov")
        data = ItemWrite(section_id=toiletries, names=_names("Plov"), price_minor=4200_00)
        with pytest.raises(AppError) as error:
            await update_item(plov, data)
    assert error.value.code == ERR_ROOMSERVICE_NOT_FOUND


async def test_unknown_ids_are_not_found(two_tenants: Tenants) -> None:
    missing = uuid.uuid4()
    with tenant_context(two_tenants[0]):
        for call in (
            rename_section(missing, SectionWrite(names=_names("X"))),
            create_item(ItemWrite(section_id=missing, names=_names("X"), price_minor=100)),
            set_available(missing, False),
            set_hidden(missing, True),
            move_section(missing, MoveDirection.UP),
            move_item(missing, MoveDirection.UP),
        ):
            with pytest.raises(AppError) as error:
                await call
            assert (error.value.code, error.value.status_code) == (ERR_ROOMSERVICE_NOT_FOUND, 404)


@pytest.mark.parametrize(
    "room_service",
    [None, room_service_config(enabled=False), room_service_config(shop=False)],
    ids=["not-configured", "disabled", "no-shop-catalog"],
)
async def test_closed_catalog_is_404(
    two_tenants: Tenants, room_service: RoomServiceConfig | None
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        toiletries = await _section(RoomServiceCatalog.SHOP, "Toiletries")
        brush = await _item(toiletries, "Toothbrush")
    await configure_tenant(tenant_a, room_service)

    with tenant_context(tenant_a):
        for call in (
            get_catalog(RoomServiceCatalog.SHOP, include_hidden=True),
            get_settings(RoomServiceCatalog.SHOP),
            set_accepting(RoomServiceCatalog.SHOP, True),
            create_section(RoomServiceCatalog.SHOP, SectionWrite(names=_names("Souvenirs"))),
            rename_section(toiletries, SectionWrite(names=_names("Hygiene"))),
            set_available(brush, False),
            set_hidden(brush, True),
            move_item(brush, MoveDirection.DOWN),
        ):
            with pytest.raises(AppError) as error:
                await call
            assert (error.value.code, error.value.status_code) == (
                ERR_ROOMSERVICE_CATALOG_DISABLED,
                404,
            )


async def test_catalogs_are_isolated_between_tenants(two_tenants: Tenants) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        mains = await _section(RoomServiceCatalog.RESTAURANT, "Mains")
        plov = await _item(mains, "Plov")
        await set_accepting(RoomServiceCatalog.RESTAURANT, True)
    with tenant_context(tenant_b):
        assert (
            await get_catalog(RoomServiceCatalog.RESTAURANT, include_hidden=True)
        ).sections == []
        assert (await get_settings(RoomServiceCatalog.RESTAURANT)).accepting_orders is False
        for call in (
            rename_section(mains, SectionWrite(names=_names("Stolen"))),
            create_item(ItemWrite(section_id=mains, names=_names("X"), price_minor=100)),
            set_hidden(plov, True),
            move_item(plov, MoveDirection.DOWN),
        ):
            with pytest.raises(AppError) as error:
                await call
            assert error.value.code == ERR_ROOMSERVICE_NOT_FOUND
    with tenant_context(tenant_a):
        assert await _staff_view(RoomServiceCatalog.RESTAURANT) == [("Mains", ["Plov"])]


@pytest.mark.parametrize(
    "price_minor", [0, -100, 150, MAX_PRICE_MINOR + 100], ids=["zero", "negative", "tiyn", "cap"]
)
def test_price_must_be_whole_tenge_within_cap(price_minor: int) -> None:
    with pytest.raises(ValidationError):
        ItemWrite(section_id=uuid.uuid4(), names=_names("Tea"), price_minor=price_minor)


def test_names_longer_than_panel_field_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SectionWrite(names=LocalizedText(en="Soups", ru="С" * 121))


def _row(name: str, position: int) -> RoomServiceSection:
    """Раздел в памяти — как его отдал `SELECT … FOR UPDATE` после ожидания."""
    return RoomServiceSection(
        id=uuid.uuid4(),
        catalog=RoomServiceCatalog.RESTAURANT,
        position=position,
        names={"en": name},
        created_at=utc_now(),
    )


def test_move_follows_positions_read_after_lock() -> None:
    """Ревью PR #508, П-1: `ORDER BY` посчитан до ожидания блокировки, а соседняя
    перестановка успела поменять позиции — сдвиг идёт по новым позициям, а не по
    порядку списка, и нажатие соседа не теряется."""
    first, second, third = _row("A", 0), _row("B", 1), _row("C", 2)
    stale_order = [second, first, third]  # так их отсортировал запрос до ожидания

    _renumber(stale_order, third.id, MoveDirection.UP)

    by_position = sorted(stale_order, key=lambda row: row.position)
    assert [row.names["en"] for row in by_position] == ["A", "C", "B"]


def test_move_of_row_that_left_the_list_is_not_found() -> None:
    """П-2: товар в ту же секунду перенесли в другой раздел — 001, а не 500."""
    with pytest.raises(AppError) as error:
        _renumber([_row("A", 0), _row("B", 1)], uuid.uuid4(), MoveDirection.DOWN)
    assert (error.value.code, error.value.status_code) == (ERR_ROOMSERVICE_NOT_FOUND, 404)
