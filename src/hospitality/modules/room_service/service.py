"""Бизнес-логика модуля room_service: каталоги (spec 0038 §4, PR 3) — копия
канона `modules/requests/service.py`.

Единственный путь читать и править каталог. Каждая функция — одна транзакция:
вызывается внутри `tenant_context`, открывает `session_scope()`. Каталог
существует для модуля, только пока он открыт конфигом тенанта
(`TenantConfig.room_service_catalog`): иначе ERR-ROOMSERVICE-002 на любом
вызове — и чтении гостя, и правке панели. Кто вправе править (`can_edit_catalog`),
решает кабинет (PR 4), как у заявок: домен держит данные, а не роли.

Событий правка каталога не публикует: подписчиков у неё нет. Заказы и их
заявки — `orders.py` (PR 5).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.room_service.models import (
    RoomServiceCatalog,
    RoomServiceItem,
    RoomServiceSection,
    RoomServiceSettings,
)
from hospitality.modules.room_service.schemas import (
    CatalogRead,
    CatalogSettingsRead,
    ItemRead,
    ItemWrite,
    MoveDirection,
    SectionRead,
    SectionWithItems,
    SectionWrite,
)
from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    RoomServiceCatalogConfig,
    TenantConfig,
    load_tenant_config,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_ROOMSERVICE_NOT_FOUND: Final = "ERR-ROOMSERVICE-001"
ERR_ROOMSERVICE_CATALOG_DISABLED: Final = "ERR-ROOMSERVICE-002"

_SECTION_ORDER = (RoomServiceSection.position, RoomServiceSection.created_at, RoomServiceSection.id)
_ITEM_ORDER = (RoomServiceItem.position, RoomServiceItem.created_at, RoomServiceItem.id)


async def get_catalog(catalog: RoomServiceCatalog, *, include_hidden: bool = False) -> CatalogRead:
    """Разделы и товары каталога по порядку.

    По умолчанию — вид гостя: без скрытых товаров и без разделов, где не
    осталось видимых. «Закончилось» гость видит (`is_available=False`) — блюдо
    в меню есть, сегодня его нет. `include_hidden=True` — вид панели: всё.
    """
    async with session_scope() as session:
        await _open_catalog(session, catalog)
        sections = (
            await session.scalars(
                select(RoomServiceSection)
                .where(RoomServiceSection.catalog == catalog)
                .order_by(*_SECTION_ORDER)
            )
        ).all()
        items_query = (
            select(RoomServiceItem)
            .where(RoomServiceItem.section_id.in_([section.id for section in sections]))
            .order_by(*_ITEM_ORDER)
        )
        if not include_hidden:
            items_query = items_query.where(RoomServiceItem.hidden_at.is_(None))
        items = (await session.scalars(items_query)).all()
    by_section: dict[uuid.UUID, list[ItemRead]] = {section.id: [] for section in sections}
    for item in items:
        by_section[item.section_id].append(ItemRead.model_validate(item))
    result: list[SectionWithItems] = []
    for section in sections:
        section_items = by_section[section.id]
        if not section_items and not include_hidden:
            continue
        read = SectionRead.model_validate(section)
        result.append(SectionWithItems(**read.model_dump(), items=section_items))
    return CatalogRead(catalog=catalog, sections=result)


async def create_section(catalog: RoomServiceCatalog, data: SectionWrite) -> SectionRead:
    """Новый раздел в конец каталога."""
    async with session_scope() as session:
        await _open_catalog(session, catalog)
        last = await session.scalar(
            select(func.max(RoomServiceSection.position)).where(
                RoomServiceSection.catalog == catalog
            )
        )
        section = RoomServiceSection(
            catalog=catalog,
            position=0 if last is None else last + 1,
            names=data.names.as_json(),
        )
        session.add(section)
        await session.flush()
        result = SectionRead.model_validate(section)
    logger.info("room_service_section_created", section_id=str(result.id), catalog=catalog.value)
    return result


async def rename_section(section_id: uuid.UUID, data: SectionWrite) -> SectionRead:
    async with session_scope() as session:
        section = await _get_section(session, section_id, for_update=True)
        await _open_catalog(session, section.catalog)
        section.names = data.names.as_json()
        await session.flush()
        result = SectionRead.model_validate(section)
    logger.info("room_service_section_renamed", section_id=str(section_id))
    return result


async def create_item(data: ItemWrite) -> ItemRead:
    """Новый товар в конец раздела `data.section_id`."""
    async with session_scope() as session:
        section = await _get_section(session, data.section_id)
        await _open_catalog(session, section.catalog)
        item = RoomServiceItem(
            section_id=section.id,
            position=await _next_item_position(session, section.id),
            names=data.names.as_json(),
            descriptions=data.descriptions.as_json() if data.descriptions else None,
            price_minor=data.price_minor,
            is_available=data.is_available,
        )
        session.add(item)
        await session.flush()
        result = ItemRead.model_validate(item)
    logger.info("room_service_item_created", item_id=str(result.id), catalog=section.catalog.value)
    return result


async def update_item(item_id: uuid.UUID, data: ItemWrite) -> ItemRead:
    """Сохранить форму товара целиком; смена раздела ставит товар в конец нового.

    Раздел другого каталога — ERR-ROOMSERVICE-001, как несуществующий: блюдо
    не становится товаром магазина (у каталогов разные службы и сборы).
    """
    async with session_scope() as session:
        item, section = await _get_item(session, item_id, for_update=True)
        await _open_catalog(session, section.catalog)
        if data.section_id != item.section_id:
            target = await _get_section(session, data.section_id)
            if target.catalog != section.catalog:
                raise _not_found(f"Section {data.section_id} is not in this catalog")
            item.section_id = target.id
            item.position = await _next_item_position(session, target.id)
        item.names = data.names.as_json()
        item.descriptions = data.descriptions.as_json() if data.descriptions else None
        item.price_minor = data.price_minor
        item.is_available = data.is_available
        await session.flush()
        result = ItemRead.model_validate(item)
    logger.info("room_service_item_updated", item_id=str(item_id))
    return result


async def set_available(item_id: uuid.UUID, available: bool) -> ItemRead:
    """«В наличии» / «Закончилось» — переключатель на карточке товара."""
    async with session_scope() as session:
        item, section = await _get_item(session, item_id, for_update=True)
        await _open_catalog(session, section.catalog)
        item.is_available = available
        await session.flush()
        result = ItemRead.model_validate(item)
    logger.info("room_service_item_availability_set", item_id=str(item_id), available=available)
    return result


async def set_hidden(item_id: uuid.UUID, hidden: bool) -> ItemRead:
    """«Скрыть из меню» / «Вернуть в меню». Повтор не сдвигает момент скрытия."""
    async with session_scope() as session:
        item, section = await _get_item(session, item_id, for_update=True)
        await _open_catalog(session, section.catalog)
        if not hidden:
            item.hidden_at = None
        elif item.hidden_at is None:
            item.hidden_at = utc_now()
        await session.flush()
        result = ItemRead.model_validate(item)
    logger.info("room_service_item_hidden_set", item_id=str(item_id), hidden=hidden)
    return result


async def move_section(section_id: uuid.UUID, direction: MoveDirection) -> SectionRead:
    """Раздел на одну строку выше или ниже; у края — без изменений."""
    async with session_scope() as session:
        section = await _get_section(session, section_id)
        await _open_catalog(session, section.catalog)
        siblings = (
            await session.scalars(
                select(RoomServiceSection)
                .where(RoomServiceSection.catalog == section.catalog)
                .order_by(*_SECTION_ORDER)
                .with_for_update()
                # Раздел уже в identity map сессии: без перечитывания его позиция
                # осталась бы прочитанной до блокировки (канон mutate_tenant_config).
                .execution_options(populate_existing=True)
            )
        ).all()
        _renumber(siblings, section_id, direction)
        await session.flush()
        result = SectionRead.model_validate(section)
    logger.info("room_service_section_moved", section_id=str(section_id), direction=direction.value)
    return result


async def move_item(item_id: uuid.UUID, direction: MoveDirection) -> ItemRead:
    """Товар на одну позицию выше или ниже внутри раздела; у края — без изменений."""
    async with session_scope() as session:
        item, section = await _get_item(session, item_id)
        await _open_catalog(session, section.catalog)
        siblings = (
            await session.scalars(
                select(RoomServiceItem)
                .where(RoomServiceItem.section_id == item.section_id)
                .order_by(*_ITEM_ORDER)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        _renumber(siblings, item_id, direction)
        await session.flush()
        result = ItemRead.model_validate(item)
    logger.info("room_service_item_moved", item_id=str(item_id), direction=direction.value)
    return result


async def get_settings(catalog: RoomServiceCatalog) -> CatalogSettingsRead:
    """Настройки каталога; строки «принимаем» нет — каталог заказов не принимает."""
    async with session_scope() as session:
        config = await _open_catalog(session, catalog)
        settings = await session.get(RoomServiceSettings, (current_tenant_id(), catalog))
        accepting = settings is not None and settings.accepting_orders
    return _settings_read(catalog, config, accepting=accepting)


async def set_accepting(catalog: RoomServiceCatalog, accepting: bool) -> CatalogSettingsRead:
    """Переключатель «Заказы в номер: принимаем» каталога."""
    async with session_scope() as session:
        config = await _open_catalog(session, catalog)
        statement = insert(RoomServiceSettings).values(
            tenant_id=current_tenant_id(),
            catalog=catalog,
            accepting_orders=accepting,
            updated_at=utc_now(),
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=[RoomServiceSettings.tenant_id, RoomServiceSettings.catalog],
                set_={"accepting_orders": accepting, "updated_at": statement.excluded.updated_at},
            )
        )
    logger.info("room_service_accepting_set", catalog=catalog.value, accepting=accepting)
    return _settings_read(catalog, config, accepting=accepting)


async def catalog_of_section(section_id: uuid.UUID) -> RoomServiceCatalog:
    """Каталог раздела — кабинету, чтобы проверить право до правки (spec 0038 Р-9).

    Каталог раздела не меняется никогда, поэтому проверка до транзакции
    действия гонки не открывает. Нет раздела — ERR-ROOMSERVICE-001.
    """
    async with session_scope() as session:
        return (await _get_section(session, section_id)).catalog


async def catalog_of_item(item_id: uuid.UUID) -> RoomServiceCatalog:
    """Каталог товара: товар переезжает только между разделами своего каталога
    (`update_item`), поэтому и он постоянен. Нет товара — ERR-ROOMSERVICE-001."""
    async with session_scope() as session:
        _, section = await _get_item(session, item_id)
        return section.catalog


def _renumber(
    siblings: Sequence[RoomServiceSection | RoomServiceItem],
    moved_id: uuid.UUID,
    direction: MoveDirection,
) -> None:
    """Сдвинуть строку на одну позицию и записать позиции 0…n-1 заново.

    Перенумерация целиком, а не обмен двух позиций: у строк, добавленных
    одновременно, позиции могут совпасть, и обмен равных ничего бы не сдвинул.
    Порядок — заново по позициям после блокировки: `ORDER BY` запроса считан
    до ожидания `FOR UPDATE`, и при двух одновременных перестановках вторая
    шла бы по устаревшему порядку и теряла первую (ревью PR #508, П-1).
    Строки нет среди соседей — её только что перенесли в другой раздел:
    «не найдена», а не 500 (П-2).
    """
    order = sorted(siblings, key=lambda row: (row.position, row.created_at, row.id))
    index = next((position for position, row in enumerate(order) if row.id == moved_id), None)
    if index is None:
        raise _not_found(f"Room service row {moved_id} is no longer in this list")
    target = index - 1 if direction is MoveDirection.UP else index + 1
    if 0 <= target < len(order):
        order[index], order[target] = order[target], order[index]
    for position, row in enumerate(order):
        if row.position != position:
            row.position = position


async def _open_catalog(
    session: AsyncSession, catalog: RoomServiceCatalog
) -> RoomServiceCatalogConfig:
    """Настройки каталога из конфига тенанта; каталог закрыт — ERR-ROOMSERVICE-002."""
    settings, _ = await open_catalog_with_config(session, catalog)
    return settings


async def open_catalog_with_config(
    session: AsyncSession, catalog: RoomServiceCatalog
) -> tuple[RoomServiceCatalogConfig, TenantConfig]:
    """Настройки каталога и весь конфиг тенанта; каталог закрыт — ERR-ROOMSERVICE-002.

    Тенант без конфига (онбординг не завершён) — тоже «закрыт»: room service у
    него не настроен. Конфиг целиком нужен заказу: пояс отеля для суточного
    лимита (`orders.py`).
    """
    config: TenantConfig | None
    try:
        config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        config = None
    settings = config.room_service_catalog(catalog.value) if config is not None else None
    if config is None or settings is None:
        raise AppError(
            code=ERR_ROOMSERVICE_CATALOG_DISABLED,
            message=f"Room service catalog {catalog.value!r} is disabled",
            status_code=404,
        )
    return settings, config


async def _get_section(
    session: AsyncSession, section_id: uuid.UUID, *, for_update: bool = False
) -> RoomServiceSection:
    query = select(RoomServiceSection).where(RoomServiceSection.id == section_id)
    if for_update:
        query = query.with_for_update()
    section = await session.scalar(query)
    if section is None:
        raise _not_found(f"Room service section {section_id} not found")
    return section


async def _get_item(
    session: AsyncSession, item_id: uuid.UUID, *, for_update: bool = False
) -> tuple[RoomServiceItem, RoomServiceSection]:
    query = select(RoomServiceItem).where(RoomServiceItem.id == item_id)
    if for_update:
        query = query.with_for_update()
    item = await session.scalar(query)
    if item is None:
        raise _not_found(f"Room service item {item_id} not found")
    return item, await _get_section(session, item.section_id)


async def _next_item_position(session: AsyncSession, section_id: uuid.UUID) -> int:
    last = await session.scalar(
        select(func.max(RoomServiceItem.position)).where(RoomServiceItem.section_id == section_id)
    )
    return 0 if last is None else last + 1


def _not_found(message: str) -> AppError:
    return AppError(code=ERR_ROOMSERVICE_NOT_FOUND, message=message, status_code=404)


def _settings_read(
    catalog: RoomServiceCatalog, config: RoomServiceCatalogConfig, *, accepting: bool
) -> CatalogSettingsRead:
    return CatalogSettingsRead(
        catalog=catalog,
        accepting_orders=accepting,
        category_key=config.category_key,
        service_charge_percent=config.service_charge_percent,
        delivery_note=config.delivery_note,
    )
