"""Фото товара (spec 0038 §6 п. 7, PR 7): поставить, заменить, убрать, выдать.

Файлы идут через настоящий узел `platform/files` (песочница — дочерний процесс)
в Fake-хранилище. Проверяется то, что обещает `photos.py`: замена удаляет
прежний файл и не трогает соседа, упавшая запись ссылки не оставляет файла,
скрытый товар гость не видит, чужой тенант — 001, закрытый каталог — 002 до
приёма файла.
"""

from __future__ import annotations

import asyncio
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import func, select

from hospitality.modules.room_service import photos, service
from hospitality.modules.room_service.api import (
    ERR_ROOMSERVICE_CATALOG_DISABLED,
    ERR_ROOMSERVICE_NOT_FOUND,
    ItemWrite,
    RoomServiceCatalog,
    SectionWrite,
    catalog_of_photo,
    create_item,
    create_section,
    get_catalog,
    read_item_photo,
    remove_item_photo,
    set_hidden,
    set_item_photo,
)
from hospitality.modules.room_service.tests.conftest import configure_tenant, room_service_config
from hospitality.platform.config import LocalizedText
from hospitality.platform.files import UploadedFile, read_file
from hospitality.platform.models import User
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from tests.test_file_content import make_png

Tenants = tuple[uuid.UUID, uuid.UUID]


@pytest.fixture
async def stand(two_tenants: Tenants, fake_file_storage: FileStoragePort) -> Tenants:
    return two_tenants


async def _item(catalog: RoomServiceCatalog = RoomServiceCatalog.RESTAURANT) -> uuid.UUID:
    section = await create_section(catalog, SectionWrite(names=LocalizedText(en="Mains")))
    item = await create_item(
        ItemWrite(section_id=section.id, names=LocalizedText(en="Lagman"), price_minor=340000)
    )
    return item.id


async def _user() -> uuid.UUID:
    async with platform_session_scope() as session:
        user = User(display_name="Айгерим")
        session.add(user)
        await session.flush()
        return user.id


async def _files_count() -> int:
    async with session_scope() as session:
        return (await session.scalar(select(func.count()).select_from(UploadedFile))) or 0


async def _code(awaitable: object) -> str:
    with pytest.raises(AppError) as error:
        await awaitable  # type: ignore[misc]
    return error.value.code


def _colour(data: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(data)) as image:
        red, green, blue = image.convert("RGB").getpixel((5, 5))  # type: ignore[misc]
    return red, green, blue


async def test_photo_is_stored_shown_in_catalog_and_read(stand: Tenants) -> None:
    user = await _user()
    with tenant_context(stand[0]):
        item_id = await _item()
        item = await set_item_photo(
            item_id, make_png((2000, 1500)), "лагман.png", uploaded_by_user_id=user
        )
        view = await get_catalog(RoomServiceCatalog.RESTAURANT)
        stored = await read_item_photo(item.photo_file_id)  # type: ignore[arg-type]
        catalog = await catalog_of_photo(item.photo_file_id)  # type: ignore[arg-type]

    assert item.photo_file_id is not None
    assert view.sections[0].items[0].photo_file_id == item.photo_file_id
    assert stored.meta.content_type == "image/webp"
    assert stored.meta.original_name == "лагман.webp"
    with Image.open(io.BytesIO(stored.data)) as image:
        assert max(image.size) == 960
    assert catalog is RoomServiceCatalog.RESTAURANT


async def test_replacing_deletes_previous_file_and_keeps_neighbour(stand: Tenants) -> None:
    """Замена удаляет только прежний файл этого товара: фото соседа читается
    (ревью PR #495, Н-3 — удаление «одного из двух»)."""
    user = await _user()
    with tenant_context(stand[0]):
        first, neighbour = await _item(), await _item()
        old = await set_item_photo(first, make_png(), "a.png", uploaded_by_user_id=user)
        kept = await set_item_photo(neighbour, make_png(), "b.png", uploaded_by_user_id=user)
        new = await set_item_photo(first, make_png((80, 60)), "c.png", uploaded_by_user_id=user)
        old_code = await _code(read_file(old.photo_file_id))  # type: ignore[arg-type]
        neighbour_file = await read_item_photo(kept.photo_file_id)  # type: ignore[arg-type]
        count = await _files_count()

    assert new.photo_file_id not in (None, old.photo_file_id)
    assert old_code == "ERR-FILES-004"
    assert neighbour_file.meta.id == kept.photo_file_id
    assert count == 2


async def test_remove_deletes_file_and_repeat_is_harmless(stand: Tenants) -> None:
    user = await _user()
    with tenant_context(stand[0]):
        item_id = await _item()
        photo = await set_item_photo(item_id, make_png(), "a.png", uploaded_by_user_id=user)
        removed = await remove_item_photo(item_id)
        again = await remove_item_photo(item_id)
        file_code = await _code(read_file(photo.photo_file_id))  # type: ignore[arg-type]
        photo_code = await _code(read_item_photo(photo.photo_file_id))  # type: ignore[arg-type]

    assert removed.photo_file_id is None and again.photo_file_id is None
    assert file_code == "ERR-FILES-004"
    assert photo_code == ERR_ROOMSERVICE_NOT_FOUND


async def test_hidden_item_photo_is_for_panel_only(stand: Tenants) -> None:
    user = await _user()
    with tenant_context(stand[0]):
        item_id = await _item()
        photo = await set_item_photo(item_id, make_png(), "a.png", uploaded_by_user_id=user)
        await set_hidden(item_id, True)
        file_id = photo.photo_file_id
        assert file_id is not None
        guest = await _code(read_item_photo(file_id))
        panel = await read_item_photo(file_id, include_hidden=True)

    assert guest == ERR_ROOMSERVICE_NOT_FOUND
    assert panel.meta.id == file_id


async def test_other_tenant_neither_reads_nor_sets_photo(stand: Tenants) -> None:
    user = await _user()
    with tenant_context(stand[0]):
        item_id = await _item()
        photo = await set_item_photo(item_id, make_png(), "a.png", uploaded_by_user_id=user)
    file_id = photo.photo_file_id
    assert file_id is not None
    with tenant_context(stand[1]):
        read = await _code(read_item_photo(file_id, include_hidden=True))
        catalog = await _code(catalog_of_photo(file_id))
        upload = await _code(set_item_photo(item_id, make_png(), "x.png", uploaded_by_user_id=user))
        removal = await _code(remove_item_photo(item_id))
        foreign_files = await _files_count()

    assert read == catalog == upload == removal == ERR_ROOMSERVICE_NOT_FOUND
    assert foreign_files == 0


async def test_closed_catalog_refuses_before_storing(stand: Tenants) -> None:
    user = await _user()
    with tenant_context(stand[0]):
        shop_item = await _item(RoomServiceCatalog.SHOP)
    await configure_tenant(stand[0], room_service_config(shop=False))
    with tenant_context(stand[0]):
        code = await _code(set_item_photo(shop_item, make_png(), "a.png", uploaded_by_user_id=user))
        count = await _files_count()

    assert code == ERR_ROOMSERVICE_CATALOG_DISABLED
    assert count == 0


async def test_rejected_file_leaves_item_and_storage_as_they_were(stand: Tenants) -> None:
    user = await _user()
    with tenant_context(stand[0]):
        item_id = await _item()
        code = await _code(
            set_item_photo(item_id, b"%PDF-1.4 not a photo", "a.pdf", uploaded_by_user_id=user)
        )
        view = await get_catalog(RoomServiceCatalog.RESTAURANT)
        count = await _files_count()

    assert code == "ERR-FILES-002"
    assert view.sections[0].items[0].photo_file_id is None
    assert count == 0


async def test_failed_link_deletes_new_file(
    stand: Tenants, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Товар пропал, пока шла проверка файла: ссылка не записалась — файла нет."""
    user = await _user()
    calls = 0
    real = service._get_item

    async def vanishing(session: object, item_id: uuid.UUID, **kwargs: bool) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise AppError(code=ERR_ROOMSERVICE_NOT_FOUND, message="gone", status_code=404)
        return await real(session, item_id, **kwargs)  # type: ignore[arg-type]

    with tenant_context(stand[0]):
        item_id = await _item()
        monkeypatch.setattr(photos, "_get_item", vanishing)
        code = await _code(set_item_photo(item_id, make_png(), "a.png", uploaded_by_user_id=user))
        count = await _files_count()

    assert code == ERR_ROOMSERVICE_NOT_FOUND
    assert count == 0


async def test_two_replacements_at_once_leave_one_file(stand: Tenants) -> None:
    """Обе замены доходят; вторая под блокировкой удаляет фото первой."""
    user = await _user()
    with tenant_context(stand[0]):
        item_id = await _item()
        first, second = await asyncio.gather(
            set_item_photo(item_id, make_png(), "a.png", uploaded_by_user_id=user),
            set_item_photo(item_id, make_png((80, 60)), "b.png", uploaded_by_user_id=user),
        )
        view = await get_catalog(RoomServiceCatalog.RESTAURANT)
        count = await _files_count()

    winner = view.sections[0].items[0].photo_file_id
    assert winner in (first.photo_file_id, second.photo_file_id)
    assert count == 1
