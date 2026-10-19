"""Данные демо-страницы «Передача смены» (issue #450) — к собранию в отеле 19.10.

Касса к сдаче смены одним кликом: бланк по структуре бумажного бланка отеля.
Два выдуманных примера смены. «Сходится» (Опера = терминалы) — формат
бумажного бланка «без разницы» (фото основателя 19.10): 01 Kaspi, Halyk,
Jusan, возврат за смену — прибавляется, итог; 02 ниже наличные одной суммой —
итог пересчёта (#459). «Не сходится» (по Jusan возврат за прошлые дни) — 01
Опера и X-отчёт по банкам, 02 сводка и наличные. У обоих 03 пересчёт по
купюрам и 04 подписи. Интеграций нет:
суммы Opera, терминалов, X-отчёта ККМ и счётной машинки выдуманы здесь, а
страница лишь «загружает» их по очереди в браузере (`static/demo_handover.js`).
Ни БД, ни событий, ни сохранения.

Проверки бланка «Не сходится» считаются здесь — правилом бумажного бланка:
«Опера − X-отчёт − возврат = 0» по карте, пересчёт наличных = X-отчёт ККМ по
наличным. У «Сходится» проверок нет, как и на бумаге. «Ручной режим» —
те же бланки с вводом сумм в браузере; формулы — атрибутами шаблона
`_handover_sheet.html`, считает `static/demo_handover.js`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Literal

from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal.demo_handover_board import build_board_context

ShiftKey = Literal["match", "mismatch"]

BANKS: Final = ("Kaspi", "Halyk", "Jusan")
# Купюры — по номиналам, мелочь — общей суммой («монеты»), как на бумаге.
DENOMINATIONS: Final = (20_000, 10_000, 5_000, 2_000, 1_000, 500)
# Источники в порядке «загрузки»: (ключ — он же `data-src` полей бланка, подпись).
SOURCES: Final = (
    ("opera", "Opera — отчёт оплат"),
    ("kaspi", "Терминал Kaspi"),
    ("halyk", "Терминал Halyk"),
    ("jusan", "Терминал Jusan"),
    ("xreport", "X-отчёт ККМ"),
    ("counter", "Счётная машинка — пересчёт по купюрам"),
)

_NBSP: Final = " "


@dataclass(frozen=True)
class _Shift:
    key: ShiftKey
    label: str
    opera: tuple[Decimal, ...]  # отчёт оплат Opera по банкам BANKS
    terminals: tuple[Decimal, ...]  # терминалы по банкам — X-отчёт ККМ по карте
    refund_card: Decimal  # «Сходится» — возврат за смену, «Не сходится» — за прошлые дни
    x_cash: Decimal  # X-отчёт ККМ, наличные
    refund_cash: Decimal  # возврат наличными
    counts: tuple[int, ...]  # купюр по DENOMINATIONS — со счётной машинки
    coins: Decimal


def _kzt(*values: str) -> tuple[Decimal, ...]:
    return tuple(Decimal(value) for value in values)


_SHIFTS: Final[tuple[_Shift, ...]] = (
    _Shift(
        key="match",
        label="Сходится",
        opera=_kzt("486250.00", "312740.00", "2847315.50"),
        terminals=_kzt("486250.00", "312740.00", "2847315.50"),
        refund_card=Decimal("18450.00"),  # прибавляется к итогу
        x_cash=Decimal("1024300.00"),
        refund_cash=Decimal("0"),
        counts=(38, 19, 11, 7, 4, 2),  # 1 024 000 + 300 монетами = X-отчёт
        coins=Decimal("300"),
    ),
    _Shift(
        key="mismatch",
        label="Не сходится",
        opera=_kzt("524900.00", "268450.00", "3912780.40"),
        terminals=_kzt("524900.00", "268450.00", "3871365.15"),
        refund_card=Decimal("41415.25"),  # ровно разница по Jusan
        x_cash=Decimal("968200.00"),
        refund_cash=Decimal("12500.00"),
        counts=(31, 24, 15, 12, 7, 4),  # 968 000 + 200 монетами = X-отчёт
        coins=Decimal("200"),
    ),
)


@dataclass(frozen=True)
class Check:
    """Строка проверки бланка: ✓ сходится или ✗ расхождение с суммой."""

    ok: bool
    text: str


def _amount(value: Decimal) -> str:
    """«1 024 400,00» — неразрывные пробелы и запятая, как на бумажном бланке."""
    text = f"{abs(value):,.2f}".replace(",", _NBSP).replace(".", ",")
    return f"−{text}" if value < 0 else text


def _tg(value: Decimal) -> str:
    return f"{_amount(value)}{_NBSP}тг"


def _signed_tg(value: Decimal) -> str:
    return ("+" if value > 0 else "−") + _tg(abs(value))


def _whole(value: int | Decimal) -> str:
    return f"{int(value):,}".replace(",", _NBSP)


def _cash_total(shift: _Shift) -> Decimal:
    bills = sum(
        (Decimal(note * count) for note, count in zip(DENOMINATIONS, shift.counts, strict=True)),
        Decimal(0),
    )
    return bills + shift.coins


def card_check(shift: _Shift) -> Check:
    """«Опера − X-отчёт − возврат = 0» — проверка бланка «Не сходится»."""
    difference = sum(shift.opera, Decimal(0)) - sum(shift.terminals, Decimal(0))
    unexplained = difference - shift.refund_card
    if unexplained == 0:
        return Check(True, "✓ сходится с учётом возврата")
    return Check(False, f"✗ не объяснено возвратом: {_signed_tg(unexplained)}")


def cash_check(shift: _Shift) -> Check:
    """Пересчёт наличных против X-отчёта ККМ по наличным."""
    difference = _cash_total(shift) - shift.x_cash
    if difference == 0:
        return Check(True, "✓ пересчёт = X-отчёт")
    word = "излишек" if difference > 0 else "недостача"
    return Check(False, f"✗ {word} {_signed_tg(difference)} к X-отчёту")


def _note(shift: _Shift) -> str:
    """Примечание к расхождению по банку: разница Опера − X-отчёт словами."""
    parts = []
    for bank, opera, terminal in zip(BANKS, shift.opera, shift.terminals, strict=True):
        if opera != terminal:
            word = "больше" if opera > terminal else "меньше"
            parts.append(
                f"{bank}: по Опере {word} на {_tg(abs(opera - terminal))} — "
                "возврат по карте за прошлые дни."
            )
    return " ".join(parts)


def _sheet(shift: _Shift) -> dict[str, Any]:
    opera_total = sum(shift.opera, Decimal(0))
    x_total = sum(shift.terminals, Decimal(0))
    card_total = x_total + shift.refund_card  # итог «Сходится»: банки + возврат
    cash_total = _cash_total(shift)
    mismatch = shift.key == "mismatch"
    checks = (card_check(shift), cash_check(shift)) if mismatch else ()
    x_card = x_total if mismatch else card_total  # X-отчёт по карте — с возвратом
    banks = [
        {"name": bank, "opera": _amount(opera), "x": _amount(terminal), "x_tg": _tg(terminal)}
        for bank, opera, terminal in zip(BANKS, shift.opera, shift.terminals, strict=True)
    ]
    bills = sum(shift.counts)
    details = {
        "opera": f"карты {_tg(opera_total)}",
        **{
            bank.lower(): _tg(terminal)
            for bank, terminal in zip(BANKS, shift.terminals, strict=True)
        },
        "xreport": f"карта {_amount(x_card)} · нал. {_tg(shift.x_cash)}",
        "counter": f"{bills} шт. + монеты · {_tg(cash_total)}",
    }
    return {
        "key": shift.key,
        "label": shift.label,
        "banks": banks,
        "opera_total": _amount(opera_total),
        "opera_total_tg": _tg(opera_total),
        "x_total": _amount(x_total),
        "difference": _tg(opera_total - x_total),
        "refund_card": _tg(shift.refund_card),
        "card_total_tg": _tg(card_total),
        "card_check": checks[0] if checks else None,
        "note": _note(shift),
        "cash_total": _tg(cash_total),
        "x_cash": _tg(shift.x_cash),
        "refund_cash": _tg(shift.refund_cash),
        "cash_with_refund": _tg(shift.x_cash + shift.refund_cash),
        "cash_check": checks[1] if checks else None,
        "counts": [str(count) for count in shift.counts],
        "sums": [
            _whole(note * count) for note, count in zip(DENOMINATIONS, shift.counts, strict=True)
        ],
        "coins": _whole(shift.coins),
        "bills": f"{bills}{_NBSP}шт.",
        "count_total": _whole(cash_total),
        "details": details,
        "problems": [check.text for check in checks if not check.ok],
    }


def build_demo_handover_context(staff: StaffContext) -> dict[str, Any]:
    """Контекст шаблона `demo_handover.html`: шапка отеля, источники, два бланка
    вкладки «Касса» и общий лист вкладки «Лист передачи» (#452)."""
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "sources": SOURCES,
        "denominations": [(note, _whole(note)) for note in DENOMINATIONS],
        "sheets": [_sheet(shift) for shift in _SHIFTS],
        "board": build_board_context(staff),
    }
