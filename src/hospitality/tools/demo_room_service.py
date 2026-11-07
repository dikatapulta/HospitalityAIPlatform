"""Room service демо-отеля (spec 0038 §6, PR 3): конфиг и каталоги для staging.

Выдуманные данные: товары и цены — из прототипа панели spec 0038, казахские
названия — черновик сессии из демо #438 (сверяет носитель), описаний на
казахском нет — гость-казах увидит английские (Р-6). Сбор ресторана 10%,
магазина 0% — не числа пилота (сбор пилота — в его файле профиля `*.local`,
Р-10); 0% у магазина показывает на staging и вторую ветку текста итога (§5).

Идемпотентно, как весь сид: конфиг дополняется, только если `room_service` в
нём нет (правка руками переживает деплой); каталог заполняется, только если в
нём нет ни одного раздела, а засеянный получает лишь разделы, вписанные в сид
позже (`DEMO_SECTIONS_ADDED_LATER`); закрытый каталог пропускается.

Фото товаров (#794) — стоковые снимки из `demo_photos/` (источники и лицензии —
`demo_photos/SOURCES.md`), только на staging демо-отеля (защиты демо-инструментов,
`demo_showcase.staging_refusals`). Фото получает товар сида без фото — по
английскому названию: загруженное менеджером не перетирается, повтор ничего не
меняет. Файл идёт каноном приёма (`set_item_photo` → `platform/files`). Сбой
фото — WARNING и дальше: деплой из-за картинки не падает.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Final

from hospitality.modules.room_service.api import (
    ERR_ROOMSERVICE_CATALOG_DISABLED,
    CatalogRead,
    ItemWrite,
    RoomServiceCatalog,
    SectionWrite,
    create_item,
    create_section,
    get_catalog,
    set_accepting,
    set_item_photo,
)
from hospitality.platform.config import (
    LocalizedText,
    RoomServiceCatalogConfig,
    RoomServiceConfig,
    TenantConfig,
    mutate_tenant_config,
)
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_showcase import staging_refusals

logger = get_logger(module=__name__)

DEMO_ROOM_SERVICE: Final = RoomServiceConfig(
    enabled=True,
    catalogs={
        "restaurant": RoomServiceCatalogConfig(
            category_key="fnb",
            service_charge_percent=Decimal("10"),
            delivery_note=LocalizedText(
                en="Cooked after payment, 30–40 min", ru="Готовим после оплаты, 30–40 мин"
            ),
        ),
        "shop": RoomServiceCatalogConfig(
            category_key="supply",
            service_charge_percent=Decimal("0"),
            delivery_note=LocalizedText(
                en="Delivered to your door within 30 min", ru="Принесём к двери номера, до 30 мин"
            ),
        ),
    },
)

# Товар: (en, ru, kk или None, цена ₸, описание en, описание ru, в наличии).
_Item = tuple[str, str, str | None, int, str, str, bool]
# Раздел: (en, ru, kk, товары).
_Section = tuple[str, str, str, tuple[_Item, ...]]

DEMO_CATALOGS: Final[dict[RoomServiceCatalog, tuple[_Section, ...]]] = {
    RoomServiceCatalog.RESTAURANT: (
        ("Breakfast", "Завтраки", "Таңғы ас", (
            ("Syrniki with sour cream", "Сырники со сметаной", "Қаймақ қосылған сырниктер", 2900,
             "Four syrniki, sour cream, berry sauce", "Четыре сырника, сметана, ягодный соус",
             True),
            ("Omelette with cheese and herbs", "Омлет с сыром и зеленью",
             "Ірімшік пен жасыл шөп қосылған омлет", 2600,
             "Three eggs, cheese, dill, toast", "Три яйца, сыр, укроп, тост", True),
            ("English breakfast", "Английский завтрак", None, 4900,
             "Eggs, bacon, sausages, beans, mushrooms, toast",
             "Яйца, бекон, сосиски, фасоль, грибы, тост", True),
        )),
        ("Soups", "Супы", "Сорпалар", (
            ("Borscht with sour cream", "Борщ со сметаной", "Қаймақ қосылған борщ", 2800,
             "Beef, beetroot, sour cream, garlic buns", "Говядина, свёкла, сметана, пампушки",
             True),
            ("Lagman noodles with beef", "Лагман", "Лағман", 3400,
             "Hand-pulled noodles, beef, vegetables", "Тянутая лапша, говядина, овощи", True),
        )),
        ("Mains", "Горячее", "Ыстық тағамдар", (
            ("Almaty-style plov", "Плов по-алматински", "Алматыша палау", 4200,
             "Rice, beef, carrots, chickpeas, raisins", "Рис, говядина, морковь, нут, изюм", True),
            ("Beshbarmak", "Бешбармак", "Бешбармақ", 5900,
             "Horse meat and beef, thin dough, broth", "Конина и говядина, тонкое тесто, сорпа",
             False),
            ("Beef steak", "Стейк из говядины", None, 8900,
             "250 g, grilled vegetables, pepper sauce", "250 г, овощи гриль, перечный соус", True),
        )),
        ("Drinks", "Напитки", "Сусындар", (
            ("Black tea, pot", "Чай чёрный, чайник", "Қара шай, шәйнек", 1200,
             "600 ml, lemon on request", "600 мл, лимон по желанию", True),
            ("Cappuccino", "Капучино", "Капучино", 1600, "300 ml", "300 мл", True),
            ("Cranberry mors", "Морс клюквенный", "Мүкжидек морсы", 1200, "500 ml", "500 мл", True),
        )),
    ),
    RoomServiceCatalog.SHOP: (
        ("Chargers and electronics", "Зарядки и электроника", "Зарядтағыштар мен электроника", (
            ("USB-C charger, 20 W", "Зарядка USB-C, 20 Вт", "USB-C зарядтағышы, 20 Вт", 6500,
             "1 m cable included", "Кабель 1 м в комплекте", True),
            ("Plug adapter", "Переходник для розетки", "Розетка адаптері", 3500,
             "EU ↔ UK / US", "EU ↔ UK / US", True),
            ("Wired earphones", "Наушники проводные", "Сымды құлаққап", 3900,
             "3.5 mm, with microphone", "3,5 мм, с микрофоном", False),
        )),
        ("Toiletries", "Гигиена", "Жеке гигиена", (
            ("Toothbrush and toothpaste", "Зубная щётка и паста", "Тіс щеткасы мен пастасы", 1500,
             "Travel kit", "Дорожный набор", True),
            ("Disposable razors, 2 pcs", "Бритва одноразовая, 2 шт.",
             "Бір реттік ұстара, 2 дана", 1200, "With shaving foam", "С пеной для бритья", True),
        )),
        ("Souvenirs", "Сувениры", "Кәдесыйлар", (
            ("“Almaty” fridge magnet", "Магнит «Алматы»", "«Алматы» магниті", 1800,
             "Ceramic", "Керамика", True),
            ("Kurt (dried cheese balls), 150 g", "Курт, 150 г", "Құрт, 150 г", 2200,
             "Gift box", "Подарочная упаковка", True),
        )),
        ("Jewelry", "Украшения", "Зергерлік бұйымдар", (
            ("Turquoise earrings", "Серьги с бирюзой", None, 38000,
             "Silver 925, natural turquoise", "Серебро 925, натуральная бирюза", True),
            ("Silver bracelet with Kazakh ornament", "Серебряный браслет с казахским орнаментом",
             "Қазақ оюы бар күміс білезік", 65000,
             "Silver 925, engraved, gift box", "Серебро 925, гравировка, подарочная коробка", True),
            ("Tumar pendant", "Кулон «Тумар»", "«Тұмар» кулоны", 45000,
             "Silver, traditional amulet shape, chain included",
             "Серебро, форма старинного оберега, с цепочкой", True),
            ("Silver ring", "Серебряное кольцо", "Күміс жүзік", 29000,
             "Silver 925, adjustable size", "Серебро 925, размер регулируется", True),
        )),
    ),
}  # fmt: skip

# Разделы, вписанные в сид после первого засева staging (#701): уже засеянный
# каталог получает их, если раздела с таким названием (en или ru) в нём нет.
# Остальные разделы досевать нельзя — переименованный руками раздел сид
# вернул бы дублем.
DEMO_SECTIONS_ADDED_LATER: Final = frozenset({"Jewelry"})

# Фото товаров сида (#794): английское название → файл в `DEMO_PHOTOS_DIR`.
# Магнит «Алматы» — без фото: на стоке магнитов Алматы нет, а магнит чужого
# города на карточке «Алматы» — неправда гостю; своё фото поставит менеджер.
DEMO_ITEMS_WITHOUT_PHOTO: Final = frozenset({"“Almaty” fridge magnet"})
DEMO_PHOTOS_DIR: Final = Path(__file__).with_name("demo_photos")
DEMO_ITEM_PHOTOS: Final[dict[str, str]] = {
    "Syrniki with sour cream": "syrniki.webp",
    "Omelette with cheese and herbs": "omelette.webp",
    "English breakfast": "english-breakfast.webp",
    "Borscht with sour cream": "borscht.webp",
    "Lagman noodles with beef": "lagman.webp",
    "Almaty-style plov": "plov.webp",
    "Beshbarmak": "beshbarmak.webp",
    "Beef steak": "steak.webp",
    "Black tea, pot": "black-tea.webp",
    "Cappuccino": "cappuccino.webp",
    "Cranberry mors": "mors.webp",
    "USB-C charger, 20 W": "usb-c-charger.webp",
    "Plug adapter": "plug-adapter.webp",
    "Wired earphones": "wired-earphones.webp",
    "Toothbrush and toothpaste": "toothbrush-kit.webp",
    "Disposable razors, 2 pcs": "razors.webp",
    "Kurt (dried cheese balls), 150 g": "kurt.webp",
    "Turquoise earrings": "turquoise-earrings.webp",
    "Silver bracelet with Kazakh ornament": "silver-bracelet.webp",
    "Tumar pendant": "tumar-pendant.webp",
    "Silver ring": "silver-ring.webp",
}


async def seed_demo_room_service(tenant_id: uuid.UUID) -> None:
    """Room service демо-отеля: конфиг, если его нет, и пустые каталоги (идемпотентно)."""
    async with platform_session_scope() as session:
        await mutate_tenant_config(session, tenant_id, _with_demo_room_service)
    with_photos = not staging_refusals(confirmed=True)
    if not with_photos:
        logger.info("demo_room_service_photos_skipped_not_staging")
    with tenant_context(tenant_id):
        for catalog, sections in DEMO_CATALOGS.items():
            await _seed_catalog(catalog, sections, with_photos=with_photos)


def _with_demo_room_service(config: TenantConfig) -> TenantConfig:
    if config.room_service is not None:
        return config
    return config.model_copy(update={"room_service": DEMO_ROOM_SERVICE})


async def _seed_catalog(
    catalog: RoomServiceCatalog, sections: tuple[_Section, ...], *, with_photos: bool
) -> None:
    try:
        existing = await get_catalog(catalog, include_hidden=True)
    except AppError as error:
        if error.code != ERR_ROOMSERVICE_CATALOG_DISABLED:
            raise
        logger.info("demo_room_service_catalog_closed", catalog=catalog.value)
        return
    if existing.sections:
        await _add_later_sections(catalog, sections, existing)
    else:
        await _create_sections(catalog, sections)
        await set_accepting(catalog, True)
        logger.info("demo_room_service_catalog_seeded", catalog=catalog.value)
    if with_photos:
        await _attach_demo_photos(catalog)


async def _add_later_sections(
    catalog: RoomServiceCatalog, sections: tuple[_Section, ...], existing: CatalogRead
) -> None:
    """Засеянный каталог: только разделы из `DEMO_SECTIONS_ADDED_LATER`, которых в нём
    нет. «Принимаем» не трогается — его могли выключить руками."""
    present = {
        name for section in existing.sections for name in (section.names.en, section.names.ru)
    }
    missing = tuple(
        section
        for section in sections
        if section[0] in DEMO_SECTIONS_ADDED_LATER and not present & {section[0], section[1]}
    )
    if not missing:
        logger.info("demo_room_service_catalog_seed_skipped", catalog=catalog.value)
        return
    await _create_sections(catalog, missing)
    logger.info(
        "demo_room_service_sections_added",
        catalog=catalog.value,
        sections=[section[0] for section in missing],
    )


async def _create_sections(catalog: RoomServiceCatalog, sections: tuple[_Section, ...]) -> None:
    for en, ru, kk, items in sections:
        section = await create_section(
            catalog, SectionWrite(names=LocalizedText(en=en, ru=ru, kk=kk))
        )
        for name_en, name_ru, name_kk, price, about_en, about_ru, available in items:
            await create_item(
                ItemWrite(
                    section_id=section.id,
                    names=LocalizedText(en=name_en, ru=name_ru, kk=name_kk),
                    descriptions=LocalizedText(en=about_en, ru=about_ru),
                    price_minor=price * 100,
                    is_available=available,
                )
            )


async def _attach_demo_photos(catalog: RoomServiceCatalog) -> None:
    """Фото товарам сида, у которых его нет; сбой одного — WARNING, остальные идут."""
    attached = 0
    for section in (await get_catalog(catalog, include_hidden=True)).sections:
        for item in section.items:
            filename = DEMO_ITEM_PHOTOS.get(item.names.en)
            if filename is None or item.photo_file_id is not None:
                continue
            try:
                data = (DEMO_PHOTOS_DIR / filename).read_bytes()
                await set_item_photo(item.id, data, filename, uploaded_by_user_id=None)
            except Exception:
                logger.warning(
                    "demo_room_service_photo_failed",
                    catalog=catalog.value,
                    item_id=str(item.id),
                    photo=filename,
                    exc_info=True,
                )
                continue
            attached += 1
    if attached:
        logger.info("demo_room_service_photos_attached", catalog=catalog.value, count=attached)
