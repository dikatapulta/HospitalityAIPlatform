"""Фото товара каталога (spec 0038 §3 и §6 п. 7, PR 7 серии; spec 0047).

Файл принимает и хранит узел ядра `platform/files.py` (назначение `menu_photo`):
проверка в песочнице, перекодировка в WebP не больше 960 px без метаданных,
шифрование. Модуль держит только id строки `uploaded_files` в
`room_service_items.photo_file_id` и решает, чей это файл и кому его показать.

Замена — по порядку: новый файл в узел → ссылка на него под `FOR UPDATE`
строки товара → удаление прежнего файла после коммита. Упала запись ссылки —
новый файл удаляется тут же. Не удалился прежний (хранилище недоступно) —
WARNING, а строка `uploaded_files` без ссылки остаётся: `menu_photo` ретеншн
не трогает (spec 0047 §6), потеряно место на томе, а не данные. Две замены
одного товара разом обе доходят: вторая под блокировкой видит фото первой и
удаляет его.

Выдача — по id файла: он же адрес картинки, и замена меняет адрес, поэтому
кэш браузера старого фото не покажет. Файл, который не фото товара этого
тенанта (или скрытого товара — для гостя), — ERR-ROOMSERVICE-001, как
несуществующий.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.room_service.models import RoomServiceCatalog, RoomServiceItem
from hospitality.modules.room_service.schemas import ItemRead
from hospitality.modules.room_service.service import (
    _get_item,
    _get_section,
    _not_found,
    _open_catalog,
)
from hospitality.platform.files import (
    FilePurpose,
    StoredFile,
    delete_file,
    read_file,
    store_upload,
)
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def set_item_photo(
    item_id: uuid.UUID, data: bytes, original_name: str, *, uploaded_by_user_id: uuid.UUID
) -> ItemRead:
    """Поставить или заменить фото товара; негодный файл — ERR-FILES-001…003.

    Товар и открытый каталог проверяются до песочницы: закрытый каталог или
    чужой товар не тратят проверку файла и не оставляют его на томе.
    """
    async with session_scope() as session:
        _, section = await _get_item(session, item_id)
        await _open_catalog(session, section.catalog)
    meta = await store_upload(
        FilePurpose.MENU_PHOTO, data, original_name, uploaded_by_user_id=uploaded_by_user_id
    )
    try:
        async with session_scope() as session:
            item, section = await _get_item(session, item_id, for_update=True)
            await _open_catalog(session, section.catalog)
            previous = item.photo_file_id
            item.photo_file_id = meta.id
            await session.flush()
            result = ItemRead.model_validate(item)
    except BaseException:
        # Товар пропал или каталог закрыли, пока шла проверка, — файл без
        # ссылки не нужен никому.
        await _delete_quietly(meta.id)
        raise
    if previous is not None:
        await _delete_quietly(previous)
    logger.info(
        "room_service_item_photo_set",
        item_id=str(item_id),
        file_id=str(meta.id),
        replaced=previous is not None,
    )
    return result


async def remove_item_photo(item_id: uuid.UUID) -> ItemRead:
    """«Убрать фото»: ссылка снимается, файл удаляется. Фото нет — без изменений."""
    async with session_scope() as session:
        item, section = await _get_item(session, item_id, for_update=True)
        await _open_catalog(session, section.catalog)
        previous = item.photo_file_id
        item.photo_file_id = None
        await session.flush()
        result = ItemRead.model_validate(item)
    if previous is not None:
        await _delete_quietly(previous)
        logger.info("room_service_item_photo_removed", item_id=str(item_id))
    return result


async def catalog_of_photo(file_id: uuid.UUID) -> RoomServiceCatalog:
    """Каталог товара с этим фото — кабинету, чтобы проверить право до выдачи.

    Файл — не фото товара этого тенанта — ERR-ROOMSERVICE-001.
    """
    async with session_scope() as session:
        item = await _item_with_photo(session, file_id, include_hidden=True)
        return (await _get_section(session, item.section_id)).catalog


async def read_item_photo(file_id: uuid.UUID, *, include_hidden: bool = False) -> StoredFile:
    """Фото товара для выдачи; по умолчанию — вид гостя: скрытого товара нет.

    Не фото товара тенанта (или скрытого — для гостя) — ERR-ROOMSERVICE-001,
    каталог закрыт — 002. Сбои хранилища — коды узла (ERR-FILES-005, -006):
    страница вместо картинки показывает заглушку.
    """
    async with session_scope() as session:
        item = await _item_with_photo(session, file_id, include_hidden=include_hidden)
        section = await _get_section(session, item.section_id)
        await _open_catalog(session, section.catalog)
    return await read_file(file_id)


async def _item_with_photo(
    session: AsyncSession, file_id: uuid.UUID, *, include_hidden: bool
) -> RoomServiceItem:
    query = select(RoomServiceItem).where(RoomServiceItem.photo_file_id == file_id)
    if not include_hidden:
        query = query.where(RoomServiceItem.hidden_at.is_(None))
    item = await session.scalar(query)
    if item is None:
        raise _not_found(f"Room service photo {file_id} not found")
    return item


async def _delete_quietly(file_id: uuid.UUID) -> None:
    """Удалить файл узла; не удалился — WARNING, а не исключение поверх исходного
    (канон `documents.delete_files_quietly`)."""
    try:
        await delete_file(file_id)
    except Exception:
        logger.warning("room_service_photo_cleanup_failed", file_id=str(file_id), exc_info=True)
