"""Демо-каталог заказов в номер (issue #438) — выдуманные данные к презентации.

Не настоящий модуль: ни таблиц, ни записи в БД, ни вызовов модели. Каталог
читают две страницы демо — «Заказать в номер» в веб-чате гостя
(`channels/web/demo_orders_page.py`) и «Заказы в номер» в кабинете
(`staff_portal/demo_orders.py`); здесь он живёт, чтобы цены и названия на двух
страницах не разошлись. Включается выключателем `ENABLE_DEMO_ORDERS`.

Цены ресторана — из одобренного прототипа spec 0038; магазин — выдумка сессии.
Казахские названия — черновик сессии, сверяет носитель
(docs/FOUNDER_REVIEW_QUEUE.md). Удаляется PR'ом настоящего room service
(серия spec 0038): у того меню ведёт F&B в кабинете, а не код.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class DemoSection(StrEnum):
    """Раздел «Заказать в номер»: у каждого своя корзина и своя служба."""

    RESTAURANT = "restaurant"
    SHOP = "shop"


@dataclass(frozen=True)
class Localized:
    """Текст на трёх языках гостевой страницы (как экран согласия веб-чата)."""

    ru: str
    kk: str
    en: str


@dataclass(frozen=True)
class DemoItem:
    key: str
    emoji: str
    price_kzt: int
    name: Localized


@dataclass(frozen=True)
class DemoCategory:
    section: DemoSection
    title: Localized
    items: tuple[DemoItem, ...]


def _item(key: str, emoji: str, price_kzt: int, ru: str, kk: str, en: str) -> DemoItem:
    return DemoItem(key=key, emoji=emoji, price_kzt=price_kzt, name=Localized(ru, kk, en))


_R = DemoSection.RESTAURANT
_S = DemoSection.SHOP

DEMO_CATALOG: Final[tuple[DemoCategory, ...]] = (
    DemoCategory(
        _R,
        Localized("Завтраки", "Таңғы ас", "Breakfast"),
        (
            _item(
                "syrniki",
                "🥞",
                2900,
                "Сырники со сметаной",
                "Қаймақ қосылған сырниктер",
                "Syrniki with sour cream",
            ),
            _item(
                "omelet",
                "🍳",
                2600,
                "Омлет с сыром и зеленью",
                "Ірімшік пен жасыл шөп қосылған омлет",
                "Omelette with cheese and herbs",
            ),
        ),
    ),
    DemoCategory(
        _R,
        Localized("Супы", "Сорпалар", "Soups"),
        (
            _item("lagman", "🍜", 3400, "Лагман", "Лағман", "Lagman noodles with beef"),
            _item(
                "borsch",
                "🍲",
                2800,
                "Борщ со сметаной",
                "Қаймақ қосылған борщ",
                "Borscht with sour cream",
            ),
        ),
    ),
    DemoCategory(
        _R,
        Localized("Горячее", "Ыстық тағамдар", "Mains"),
        (
            _item("plov", "🍛", 4200, "Плов по-алматински", "Алматыша палау", "Almaty-style plov"),
            _item("beshbarmak", "🍖", 5900, "Бешбармак", "Бешбармақ", "Beshbarmak"),
            _item(
                "carbonara", "🍝", 3900, "Паста карбонара", "Карбонара пастасы", "Pasta carbonara"
            ),
            _item(
                "club",
                "🥪",
                3500,
                "Клаб-сэндвич с картофелем фри",
                "Фри картобы қосылған клаб-сэндвич",
                "Club sandwich with fries",
            ),
        ),
    ),
    DemoCategory(
        _R,
        Localized("Салаты и десерты", "Салаттар мен десерттер", "Salads and desserts"),
        (
            _item(
                "caesar",
                "🥗",
                3600,
                "Цезарь с курицей",
                "Тауық еті қосылған «Цезарь»",
                "Chicken Caesar salad",
            ),
            _item("medovik", "🍰", 2200, "Медовик", "Бал торты", "Honey cake"),
            _item(
                "baursak",
                "🍯",
                1500,
                "Баурсаки с мёдом",
                "Бал қосылған бауырсақ",
                "Baursaks with honey",
            ),
        ),
    ),
    DemoCategory(
        _R,
        Localized("Напитки", "Сусындар", "Drinks"),
        (
            _item("tea", "🫖", 1200, "Чай чёрный, чайник", "Қара шай, шәйнек", "Black tea, pot"),
            _item(
                "milk_tea",
                "🍵",
                1400,
                "Чай с молоком по-казахски",
                "Қазақша сүтті шай",
                "Kazakh tea with milk",
            ),
            _item("cappuccino", "☕", 1600, "Капучино", "Капучино", "Cappuccino"),
            _item("mors", "🧃", 1200, "Морс клюквенный", "Мүкжидек морсы", "Cranberry mors"),
        ),
    ),
    DemoCategory(
        _S,
        Localized(
            "Зарядки и электроника", "Зарядтағыштар мен электроника", "Chargers and electronics"
        ),
        (
            _item(
                "charger_usbc",
                "⚡",
                5900,
                "Зарядка USB-C, 20 Вт",
                "USB-C зарядтағышы, 20 Вт",
                "USB-C charger, 20 W",
            ),
            _item(
                "charger_lightning",
                "📱",
                5900,
                "Зарядка Lightning для iPhone",
                "iPhone-ға арналған Lightning зарядтағышы",
                "Lightning charger for iPhone",
            ),
            _item(
                "plug_adapter",
                "🔌",
                2500,
                "Переходник для розетки (вилки UK, US, CN)",
                "Розетка адаптері (UK, US, CN ашалары)",
                "Plug adapter (UK, US, CN plugs)",
            ),
            _item(
                "earphones", "🎧", 3900, "Наушники проводные", "Сымды құлаққап", "Wired earphones"
            ),
        ),
    ),
    DemoCategory(
        _S,
        Localized("Гигиена", "Жеке гигиена", "Toiletries"),
        (
            _item(
                "toothbrush",
                "🪥",
                1200,
                "Зубная щётка и паста",
                "Тіс щеткасы мен пастасы",
                "Toothbrush and toothpaste",
            ),
            _item(
                "razor",
                "🪒",
                1500,
                "Бритва одноразовая, 2 шт.",
                "Бір реттік ұстара, 2 дана",
                "Disposable razors, 2 pcs",
            ),
        ),
    ),
    DemoCategory(
        _S,
        Localized("Вода и снеки", "Су және тіскебасарлар", "Water and snacks"),
        (
            _item(
                "water_still",
                "💧",
                500,
                "Вода без газа, 0,5 л",
                "Газсыз су, 0,5 л",
                "Still water, 0.5 L",
            ),
            _item(
                "water_sparkling",
                "🫧",
                500,
                "Вода с газом, 0,5 л",
                "Газдалған су, 0,5 л",
                "Sparkling water, 0.5 L",
            ),
            _item(
                "kurt", "🧀", 900, "Курт, 100 г", "Құрт, 100 г", "Kurt (dried cheese balls), 100 g"
            ),
            _item("nuts", "🥜", 1400, "Орешки, 100 г", "Жаңғақтар, 100 г", "Mixed nuts, 100 g"),
            _item(
                "chocolate",
                "🍫",
                1200,
                "Казахстанский шоколад",
                "Қазақстандық шоколад",
                "Kazakhstani chocolate",
            ),
        ),
    ),
    DemoCategory(
        _S,
        Localized("Сувениры", "Кәдесыйлар", "Souvenirs"),
        (
            _item(
                "magnet",
                "🧲",
                1500,
                "Магнит «Алматы»",
                "«Алматы» магниті",
                "“Almaty” fridge magnet",
            ),
            _item(
                "postcards",
                "🏔️",
                2000,
                "Открытки «Горы Алматы», 5 шт.",
                "«Алматы таулары» ашықхаттары, 5 дана",
                "“Almaty mountains” postcards, set of 5",
            ),
            _item(
                "pastila",
                "🍎",
                2400,
                "Пастила из алматинского апорта",
                "Алматы апортынан жасалған пастила",
                "Almaty Aport apple pastila",
            ),
            _item("dombra", "🪕", 6500, "Мини-домбра", "Шағын домбыра", "Mini dombra"),
            _item(
                "scarf",
                "🧣",
                7900,
                "Платок с казахским орнаментом",
                "Қазақ оюы бар орамал",
                "Scarf with Kazakh ornament",
            ),
        ),
    ),
)

DEMO_ITEMS: Final[dict[str, DemoItem]] = {
    item.key: item for category in DEMO_CATALOG for item in category.items
}


def format_kzt(amount: int) -> str:
    """«5 720 ₸» — неразрывные пробелы, как `Intl.NumberFormat("ru-RU")` на
    гостевой странице (spec 0038 §8: ru и kk — пробел, en — запятая)."""
    return f"{amount:,}".replace(",", " ") + " ₸"
