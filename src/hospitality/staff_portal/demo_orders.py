"""Данные демо-страницы «Заказы в номер» (issue #438) — к презентации отелю.

Шесть выдуманных заказов: три ресторана (служба F&B) и три магазина (служба
«Снабжение»). Ни БД, ни событий: кнопки «Принял» → «Оплачено» → «Доставлено»
меняют статус только в браузере (`static/demo_orders.js`), обновление страницы
возвращает всё как было. Названия и цены — общий демо-каталог
(`channels/common/demo_orders.py`): те же, что видит гость на своей странице.
Формат карточки и подсказка под «Оплачено» — из spec 0038 §8.3. Удаляется
PR'ом настоящего room service (серия spec 0038).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from hospitality.channels.common.demo_orders import DEMO_ITEMS, DemoSection, format_kzt
from hospitality.platform.staff_auth import StaffContext

DemoStage = Literal["new", "accepted", "paid", "delivered"]


@dataclass(frozen=True)
class _DemoOrder:
    number: int
    section: DemoSection
    room: str
    stage: DemoStage
    minutes_ago: int
    payment: Literal["cash", "card"]
    lines: tuple[tuple[str, int], ...]  # (ключ позиции каталога, количество)
    note: str = ""


_R = DemoSection.RESTAURANT
_S = DemoSection.SHOP

_ORDERS: Final[tuple[_DemoOrder, ...]] = (
    _DemoOrder(12, _R, "512", "new", 4, "cash", (("lagman", 1), ("milk_tea", 1)), "без лука"),
    _DemoOrder(11, _R, "304", "accepted", 12, "card", (("plov", 2), ("tea", 1))),
    _DemoOrder(10, _R, "718", "paid", 26, "card", (("caesar", 1), ("cappuccino", 2))),
    _DemoOrder(7, _S, "215", "new", 2, "card", (("charger_usbc", 1), ("water_still", 2))),
    _DemoOrder(6, _S, "409", "accepted", 9, "cash", (("toothbrush", 2), ("razor", 1))),
    _DemoOrder(
        5,
        _S,
        "621",
        "new",
        5,
        "card",
        (("magnet", 2), ("pastila", 1), ("postcards", 1)),
        "упакуйте как подарок",
    ),
)

_EMOJI: Final = {_R: "🍽", _S: "🛍"}
_SERVICE: Final = {_R: "F&B", _S: "Снабжение"}
_PAID_HINT: Final = {
    _R: "Нажимайте, когда деньги у вас. Затем пробейте заказ в кассе ресторана.",
    _S: "Нажимайте, когда деньги у вас.",
}
_PAYMENT: Final = {"cash": "наличными", "card": "картой — возьмите терминал"}

# Бейдж стадии: подпись и тон канона очереди (`.badge-new` / `.badge-in_progress`;
# доставленный — нейтральный `.badge`).
STAGE_BADGES: Final[tuple[tuple[DemoStage, str, str], ...]] = (
    ("new", "Новый", "badge-new"),
    ("accepted", "Принят", "badge-in_progress"),
    ("paid", "Оплачено", "badge-in_progress"),
    ("delivered", "Доставлен", ""),
)
# Кнопка следующего шага: (стадия, на которой видна, стадия после нажатия, надпись).
STEP_BUTTONS: Final[tuple[tuple[DemoStage, DemoStage, str], ...]] = (
    ("new", "accepted", "Принял"),
    ("accepted", "paid", "Оплачено"),
    ("paid", "delivered", "Доставлено"),
)
FILTERS: Final = (
    ("all", "Все"),
    (_R.value, "🍽 F&B"),
    (_S.value, "🛍 Снабжение"),
)


def _card(order: _DemoOrder) -> dict[str, Any]:
    lines = [(DEMO_ITEMS[key], quantity) for key, quantity in order.lines]
    total = sum(item.price_kzt * quantity for item, quantity in lines)
    return {
        "section": order.section.value,
        "stage": order.stage,
        "head": f"{_EMOJI[order.section]} Заказ #{order.number} · № {order.room} · "
        f"{format_kzt(total)}",
        "meta": f"Служба: {_SERVICE[order.section]} · {order.minutes_ago} мин назад",
        "lines": [
            f"{item.name.ru} ×{quantity} — {format_kzt(item.price_kzt * quantity)}"
            for item, quantity in lines
        ],
        "payment": f"Оплата при получении: {_PAYMENT[order.payment]}",
        "note": order.note,
        "paid_hint": _PAID_HINT[order.section],
    }


def build_demo_orders_context(staff: StaffContext) -> dict[str, Any]:
    """Контекст шаблона `demo_orders.html`: шапка отеля и карточки заказов."""
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "filters": FILTERS,
        "stage_badges": STAGE_BADGES,
        "step_buttons": STEP_BUTTONS,
        "orders": [_card(order) for order in _ORDERS],
    }
