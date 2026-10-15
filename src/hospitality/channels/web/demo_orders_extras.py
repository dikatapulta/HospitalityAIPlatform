"""Витрина гостевой страницы демо «Заказать в номер» (issue #438, дизайн — #440).

Общий каталог (`channels/common/demo_orders.py`) держит то, что видят обе страницы
демо: название, цену, эмодзи. Здесь — только то, что нужно витрине гостя:
описание и вес/объём для карточки и листа товара, бейдж, два цвета плашки под
эмодзи; у раздела — эмодзи баннера, лента «Популярное», «Добавить к заказу» и
ETA по шагам. Данные выдуманные; kk — черновик сессии, сверяет носитель
(docs/FOUNDER_REVIEW_QUEUE.md). Удаляется вместе с демо PR'ом настоящего room
service (серия spec 0038).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from hospitality.channels.common.demo_orders import DemoSection, Localized

Badge = Literal["hit", "new", "local"]


@dataclass(frozen=True)
class ItemLook:
    desc: Localized
    colors: tuple[str, str]  # градиент плашки под эмодзи, "#rrggbb"
    meta: Localized | None = None  # вес или объём; у товаров с объёмом в названии — нет
    badge: Badge | None = None


@dataclass(frozen=True)
class SectionLook:
    art: tuple[str, ...]  # эмодзи на баннере раздела
    popular: tuple[str, ...]  # ключи позиций ленты «Популярное»
    upsell: tuple[str, ...]  # «Добавить к заказу» в корзине
    eta_steps: tuple[int, int, int]  # «будет через N мин» на шагах «Принят», «Оплачено», «Несут»


def _look(
    colors: tuple[str, str],
    ru: str,
    kk: str,
    en: str,
    *,
    meta: Localized | None = None,
    badge: Badge | None = None,
) -> ItemLook:
    return ItemLook(desc=Localized(ru, kk, en), colors=colors, meta=meta, badge=badge)


def _g(grams: int) -> Localized:
    return Localized(f"{grams} г", f"{grams} г", f"{grams} g")


def _ml(millilitres: int) -> Localized:
    return Localized(f"{millilitres} мл", f"{millilitres} мл", f"{millilitres} ml")


_CABLE = Localized("Кабель 1 м", "Кабель 1 м", "1 m cable")

ITEM_LOOKS: Final[dict[str, ItemLook]] = {
    # --- ресторан ---
    "syrniki": _look(
        ("#fde7c3", "#f4b971"),
        "Четыре сырника, сметана и ягодный соус",
        "Төрт сырник, қаймақ және жидек тұздығы",
        "Four syrniki with sour cream and berry sauce",
        meta=_g(250),
        badge="hit",
    ),
    "omelet": _look(
        ("#fff1b8", "#f5cf5b"),
        "Три яйца, сыр, зелень и тост",
        "Үш жұмыртқа, ірімшік, көк шөп және тост",
        "Three eggs, cheese, herbs and toast",
        meta=_g(280),
    ),
    "lagman": _look(
        ("#ffd9b8", "#ec9a5a"),
        "Тянутая лапша, говядина, овощи",
        "Созба кеспе, сиыр еті, көкөністер",
        "Hand-pulled noodles, beef and vegetables",
        meta=_g(400),
        badge="hit",
    ),
    "borsch": _look(
        ("#ffc9c9", "#d9505e"),
        "Говядина, свёкла, сметана и чесночные пампушки",
        "Сиыр еті, қызылша, қаймақ және сарымсақты тоқаштар",
        "Beef, beetroot, sour cream and garlic buns",
        meta=_g(350),
    ),
    "plov": _look(
        ("#ffe2a6", "#e3a33b"),
        "Рис, говядина, морковь, нут и изюм",
        "Күріш, сиыр еті, сәбіз, ноқат және мейіз",
        "Rice, beef, carrots, chickpeas and raisins",
        meta=_g(350),
        badge="hit",
    ),
    "beshbarmak": _look(
        ("#f7d4b5", "#c47c4c"),
        "Конина и говядина, тонкое тесто, сорпа",
        "Жылқы және сиыр еті, жұқа қамыр, сорпа",
        "Horse meat and beef, thin pasta, broth",
        meta=_g(450),
        badge="local",
    ),
    "carbonara": _look(
        ("#fff0c2", "#e8b850"),
        "Спагетти, бекон, пармезан и желток",
        "Спагетти, бекон, пармезан және сарыуыз",
        "Spaghetti, bacon, parmesan and egg yolk",
        meta=_g(320),
    ),
    "club": _look(
        ("#f9e3bd", "#d6a25c"),
        "Курица, бекон, яйцо, томат и картофель фри",
        "Тауық еті, бекон, жұмыртқа, қызанақ және фри",
        "Chicken, bacon, egg, tomato and fries",
        meta=_g(380),
    ),
    "caesar": _look(
        ("#dcf2c4", "#8cc463"),
        "Романо, курица гриль, пармезан, гренки",
        "Романо, гриль тауық, пармезан, кептірілген нан",
        "Romaine, grilled chicken, parmesan, croutons",
        meta=_g(260),
        badge="hit",
    ),
    "medovik": _look(
        ("#fbe6c8", "#dcae78"),
        "Домашний медовый торт со сметанным кремом",
        "Қаймақ кремі бар үй бал торты",
        "Homemade honey cake with sour cream frosting",
        meta=_g(150),
    ),
    "baursak": _look(
        ("#fde5b4", "#dba04a"),
        "Восемь баурсаков, мёд и сливочное масло",
        "Сегіз бауырсақ, бал және сары май",
        "Eight baursaks with honey and butter",
        meta=_g(200),
        badge="local",
    ),
    "tea": _look(
        ("#efdcc8", "#b07c52"),
        "Листовой чёрный чай, лимон по желанию",
        "Жапырақты қара шай, қаласаңыз лимонмен",
        "Loose-leaf black tea, lemon on request",
        meta=_ml(600),
    ),
    "milk_tea": _look(
        ("#f3e6d2", "#c9a271"),
        "Крепкий чай со сливками, как дома",
        "Кілегей қосылған қою шай, үйдегідей",
        "Strong tea with cream, the Kazakh way",
        meta=_ml(600),
        badge="local",
    ),
    "cappuccino": _look(
        ("#ead6c2", "#93613d"),
        "Двойной эспрессо и молочная пена",
        "Қос эспрессо және сүт көбігі",
        "Double espresso with milk foam",
        meta=_ml(300),
    ),
    "mors": _look(
        ("#ffc5d0", "#cf4d6c"),
        "Клюквенный морс собственного приготовления",
        "Өзіміз дайындаған мүкжидек морсы",
        "House-made cranberry drink",
        meta=_ml(500),
    ),
    # --- магазин ---
    "charger_usbc": _look(
        ("#d9e8ff", "#6f9cf2"),
        "Быстрая зарядка для Android и iPhone 15 и новее",
        "Android және iPhone 15 пен жаңаларына жылдам зарядтау",
        "Fast charging for Android and iPhone 15 or newer",
        meta=_CABLE,
        badge="hit",
    ),
    "charger_lightning": _look(
        ("#e3e3f0", "#8f8fb8"),
        "Блок и кабель для iPhone 14 и старше",
        "iPhone 14 және одан ескілеріне блок пен кабель",
        "Plug and cable for iPhone 14 or older",
        meta=_CABLE,
    ),
    "plug_adapter": _look(
        ("#e4ecef", "#8aa3ad"),
        "Для розеток Казахстана (тип C и F)",
        "Қазақстан розеткаларына арналған (C және F түрі)",
        "For Kazakhstan sockets (type C and F)",
    ),
    "earphones": _look(
        ("#e6e0f7", "#9a83d9"),
        "Разъём 3,5 мм, микрофон и пульт",
        "3,5 мм ұяшық, микрофон және пульт",
        "3.5 mm jack with mic and remote",
        meta=Localized("1,2 м", "1,2 м", "1.2 m"),
    ),
    "toothbrush": _look(
        ("#d7f3f1", "#5fc2b8"),
        "Мягкая щётка и паста 20 мл",
        "Жұмсақ щетка және 20 мл паста",
        "Soft brush and 20 ml toothpaste",
    ),
    "razor": _look(
        ("#e2eaf2", "#7d98b5"),
        "Три лезвия и увлажняющая полоска",
        "Үш жүз және ылғалдандыратын жолақ",
        "Three blades with a moisture strip",
    ),
    "water_still": _look(
        ("#d8efff", "#62b3ea"),
        "Горная вода из Алматинской области",
        "Алматы облысының тау суы",
        "Mountain water from the Almaty region",
        badge="hit",
    ),
    "water_sparkling": _look(
        ("#dff4fb", "#71c3dc"),
        "Минеральная, слабогазированная",
        "Минералды, сәл газдалған",
        "Lightly sparkling mineral water",
    ),
    "kurt": _look(
        ("#fff6dc", "#e1c47d"),
        "Солёные сырные шарики — классика степи",
        "Тұзды ірімшік домалақтары — дала классикасы",
        "Salty dried-cheese balls, a steppe classic",
        badge="local",
    ),
    "nuts": _look(
        ("#f5e1c4", "#c99a5b"),
        "Миндаль, кешью и фундук",
        "Бадам, кешью және орман жаңғағы",
        "Almonds, cashews and hazelnuts",
    ),
    "chocolate": _look(
        ("#ecd3c1", "#8a5537"),
        "Молочный шоколад местной фабрики",
        "Жергілікті фабриканың сүтті шоколады",
        "Milk chocolate from a local factory",
        meta=_g(100),
    ),
    "magnet": _look(
        ("#ffe0d1", "#ef8a63"),
        "Горы Алматы и каток Медеу",
        "Алматы таулары және Медеу мұз айдыны",
        "Almaty mountains and the Medeu rink",
    ),
    "postcards": _look(
        ("#dbe9ff", "#7aa0de"),
        "Фотооткрытки с видами Заилийского Алатау",
        "Іле Алатауы көріністері бар фотоашықхаттар",
        "Photo postcards of the Trans-Ili Alatau",
    ),
    "pastila": _look(
        ("#ffdcd6", "#e0665b"),
        "Яблочная пастила без сахара",
        "Қантсыз алма пастиласы",
        "Sugar-free apple pastila",
        meta=_g(150),
        badge="local",
    ),
    "dombra": _look(
        ("#f7e3c6", "#b98146"),
        "Сувенирная домбра 40 см — на ней можно играть",
        "40 см кәдесый домбыра — ойнауға болады",
        "40 cm souvenir dombra — you can play it",
        badge="new",
    ),
    "scarf": _look(
        ("#ffd9e6", "#d3608c"),
        "Шёлковый платок с национальным орнаментом",
        "Ұлттық оюы бар жібек орамал",
        "Silk scarf with a national ornament",
    ),
}

SECTION_LOOKS: Final[dict[DemoSection, SectionLook]] = {
    DemoSection.RESTAURANT: SectionLook(
        art=("🍜", "🥗", "🍰"),
        popular=("lagman", "plov", "syrniki", "caesar", "beshbarmak"),
        upsell=("milk_tea", "cappuccino", "mors", "medovik", "baursak"),
        eta_steps=(30, 20, 5),
    ),
    DemoSection.SHOP: SectionLook(
        art=("🔌", "💧", "🧲"),
        popular=("charger_usbc", "water_still", "plug_adapter", "kurt", "magnet"),
        upsell=("water_still", "chocolate", "nuts", "kurt"),
        eta_steps=(12, 8, 3),
    ),
}
