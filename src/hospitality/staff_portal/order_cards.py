"""Карточка заказа в номер в очереди кабинета (spec 0038 §5, §6 PR 6; issue #418).

Заказ — заявка службы каталога, но карточка у него своя: «🍽 Заказ #12 · № 512 ·
5 720 ₸», состав и суммы, пожелания, кто и когда нажал шаг, кнопка только
следующего шага — «Принял» → «Оплачено» → «Доставлено» — и «Отменить» с причиной.
Общих «Готово» с примечанием, «Вернуть в очередь», «Передать» и «Отменить» с
запиской у заказа нет: записку гость не прочтёт (ревью PR #520, Н-10), а
примечание к «Доставлено» — «выполнено с оговоркой», которое spec 0030 признала
неправдой (Н-9). Данные — `room_service.api`, сборку очереди ведёт `queue.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.config import TenantConfig

Catalog = room_service_api.RoomServiceCatalog
Reason = room_service_api.OrderCancelReason
Stage = room_service_api.OrderStage

_ICONS: Final[dict[Catalog, str]] = {Catalog.RESTAURANT: "🍽", Catalog.SHOP: "🛍"}
# Язык названий в кабинете: персонал читает по-русски, перевода нет — EN
# (тот же выбор, что у панели «Товары и меню»).
_STAFF_LANGUAGE: Final = "ru"
_NBSP: Final = "\u00a0"
_STAGE_LABELS: Final[dict[Stage, str]] = {
    Stage.SENT: "новый",
    Stage.ACCEPTED: "принят",
    Stage.PAID: "оплачен",
    Stage.DELIVERED: "доставлен",
    Stage.CANCELLED: "отменён",
}
OVERDUE_LABEL: Final = "просрочен"
_PAID_HINTS: Final[dict[Catalog, str]] = {
    Catalog.RESTAURANT: "Нажимайте, когда деньги у вас. Затем пробейте заказ в кассе ресторана",
    Catalog.SHOP: "Нажимайте, когда деньги у вас. Товар отдавайте после оплаты",
}
_REFUND_HINTS: Final[dict[Catalog, str]] = {
    Catalog.RESTAURANT: "Оплата отмечена — верните деньги гостю через кассу ресторана",
    Catalog.SHOP: "Оплата отмечена — верните деньги гостю",
}
# Порядок — порядок списка «Причина» (spec 0038 §5); «Нет блюда / товара» — по каталогу.
_REASON_LABELS: Final[tuple[tuple[Reason, str, str], ...]] = (
    (Reason.NOT_ORDERED, "Гость не заказывал", "Гость не заказывал"),
    (Reason.NO_ANSWER, "Не открыли дверь", "Не открыли дверь"),
    (Reason.PAYMENT_REFUSED, "Отказался платить", "Отказался платить"),
    (Reason.ITEM_UNAVAILABLE, "Нет блюда", "Нет товара"),
    (Reason.OTHER, "Другое", "Другое"),
)


def order_card(
    order: room_service_api.OrderRead,
    request: requests_api.ServiceRequestRead,
    config: TenantConfig | None,
    *,
    is_overdue: bool,
) -> dict[str, Any]:
    """Поля карточки заказа поверх общей карточки очереди (`queue._card`)."""
    stage = order.stage
    is_open = stage not in (Stage.DELIVERED, Stage.CANCELLED)
    is_new = request.status is requests_api.RequestStatus.NEW
    is_paid = order.paid_at is not None
    zone: tzinfo = config.tzinfo if config is not None else UTC
    number = f"#{order.number}" if order.number is not None else ""
    title = " · ".join(
        part
        for part in (
            f"{_ICONS[order.catalog]} Заказ {number}".rstrip(),
            f"№ {order.room_number}" if order.room_number else "",
            money_label(order.total_minor),
        )
        if part
    )
    return {
        "title": title,
        "badge_label": OVERDUE_LABEL if is_overdue else _STAGE_LABELS[stage],
        "lines": [
            f"{line.names.text_for(_STAFF_LANGUAGE)} ×{line.quantity} — "
            f"{money_label(line.line_total_minor)}"
            for line in order.lines
        ],
        "totals": _totals(order),
        "comment": order.comment,
        "steps": _steps(order, request, zone),
        # Следующий шаг — один: оплаченный, но возвращённый в очередь (spec 0042)
        # сначала снова берут, «Доставлено» — только взятому.
        "can_accept": is_open and is_new,
        "can_mark_paid": is_open and not is_new and not is_paid,
        "can_deliver": is_open and not is_new and is_paid,
        "paid_hint": _PAID_HINTS[order.catalog],
        "can_cancel": is_open,
        "refund_hint": _REFUND_HINTS[order.catalog] if is_paid else None,
        # Отметку оплаты не снять (ревью PR #497, Н-7): у оплаченного «Отказался платить» нет.
        "reasons": [
            {"value": reason.value, "label": shop if order.catalog is Catalog.SHOP else restaurant}
            for reason, restaurant, shop in _REASON_LABELS
            if not (is_paid and reason is Reason.PAYMENT_REFUSED)
        ],
    }


def _totals(order: room_service_api.OrderRead) -> str:
    """«Сумма 7 700 ₸ · сбор 10% — 770 ₸ · итого 8 470 ₸»; без сбора — «Итого 7 700 ₸»."""
    if order.service_charge_minor == 0:
        return f"Итого {money_label(order.total_minor)}"
    percent = room_service_api.format_percent(order.service_charge_percent).replace(".", ",")
    return (
        f"Сумма {money_label(order.subtotal_minor)} · сбор {percent}% — "
        f"{money_label(order.service_charge_minor)} · итого {money_label(order.total_minor)}"
    )


def _steps(
    order: room_service_api.OrderRead, request: requests_api.ServiceRequestRead, zone: tzinfo
) -> list[str]:
    """Кто и когда нажал шаг: «Принял: Айгерим, 14:05» — время отеля."""
    steps = []
    if request.claimed_at is not None:
        steps.append(_step("Принял", request.claimed_by_display_name, request.claimed_at, zone))
    if request.paid_at is not None:
        steps.append(_step("Оплачено", request.paid_by_display_name, request.paid_at, zone))
    if request.closed_at is not None:
        if order.stage is Stage.DELIVERED:
            label = "Доставлено"
        elif order.cancel_reason is Reason.BY_GUEST:
            label = "Гость отменил"
        else:
            label = "Отменён"
        step = _step(label, request.closed_by_display_name, request.closed_at, zone)
        reason = _reason_label(order)
        steps.append(f"{step} · {reason}" if reason else step)
    return steps


def _reason_label(order: room_service_api.OrderRead) -> str | None:
    """Причина отмены сотрудником словами списка «Причина»; гость сам или без причины — нет."""
    for reason, restaurant, shop in _REASON_LABELS:
        if order.cancel_reason is reason:
            return shop if order.catalog is Catalog.SHOP else restaurant
    return None


def _step(label: str, name: str | None, moment: datetime, zone: tzinfo) -> str:
    when = moment.astimezone(zone).strftime("%H:%M")
    return f"{label}: {name}, {when}" if name else f"{label}: {when}"


def money_label(minor: int) -> str:
    """«5 720 ₸» с неразрывными пробелами — как цена в панели «Товары и меню»
    (`catalog.money_label`; импортировать её отсюда — круг `catalog` → `queue`)."""
    return room_service_api.format_amount(minor, _STAFF_LANGUAGE).replace(" ", _NBSP)
