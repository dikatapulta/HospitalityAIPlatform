"""Данные вкладки «Лист передачи» демо «Передача смены» (issue #452).

Общий лист хвостов смены вместо «жёлтого листа» на бумаге: строки по номерам
(оплата потом, депозит, штраф, поздний выезд, другое), сумма в ₸ или $,
статус «открыт / закрыт», история «кто что изменил» и примечания внутри
строки. Строки, коллеги (DEMO1…DEMO3) и их правки выдуманы здесь; правки и
примечания вошедшего живут только в браузере до перезагрузки страницы
(`static/demo_handover_board.js`) — ни БД, ни событий, ни сохранения.

Время записей — «сколько минут назад» (`ago`): часы ставит JS по часам
компьютера на стойке, как дату и смену бланка кассы (#450).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from hospitality.platform.staff_auth import StaffContext

Currency = Literal["KZT", "USD"]

# (ключ, подпись) — порядок пунктов в списке «Что» правки; цвет метки — в CSS.
KINDS: Final = (
    ("pay_later", "Оплата потом"),
    ("deposit", "Депозит"),
    ("fine", "Штраф"),
    ("late", "Поздний выезд"),
    ("other", "Другое"),
)
CURRENCIES: Final[tuple[tuple[Currency, str], ...]] = (("KZT", "₸"), ("USD", "$"))
CREATED: Final = "внёс строку"

_NBSP: Final = "\u00a0"
_SIGN: Final[dict[Currency, str]] = dict(CURRENCIES)


@dataclass(frozen=True)
class _Entry:
    """Запись истории или примечание: кто, сколько минут назад, что."""

    login: str
    ago: int
    text: str


@dataclass(frozen=True)
class _Row:
    key: str
    room: str
    kind: str
    summary: str
    amount: int | None  # None — хвост без суммы (забытая вещь)
    currency: Currency
    closed: bool
    author: str
    ago: int
    changes: tuple[_Entry, ...] = ()
    notes: tuple[_Entry, ...] = ()


def _group(value: int) -> str:
    return f"{value:,}".replace(",", _NBSP)


def money(amount: int | None, currency: Currency) -> str:
    """«20 000 ₸», «300 $»; без суммы — «—»."""
    if amount is None:
        return "—"
    return f"{_group(amount)}{_NBSP}{_SIGN[currency]}"


_ROWS: Final[tuple[_Row, ...]] = (
    _Row(
        key="r1",
        room="1204",
        kind="pay_later",
        summary="Мини-бар и прачечная — оплатит при выезде",
        amount=20_000,
        currency="KZT",
        closed=False,
        author="DEMO1",
        ago=320,
        changes=(_Entry("DEMO2", 95, f"сумма 15{_NBSP}000 → 20{_NBSP}000{_NBSP}₸"),),
        notes=(_Entry("DEMO2", 93, "Добавилась прачечная, гость в курсе."),),
    ),
    _Row(
        key="r2",
        room="0817",
        kind="deposit",
        summary="Залог за номер — вернуть при выезде наличными",
        amount=300,
        currency="USD",
        closed=False,
        author="DEMO3",
        ago=410,
        notes=(_Entry("DEMO1", 200, "Деньги в сейфе, конверт подписан номером."),),
    ),
    _Row(
        key="r3",
        room="1502",
        kind="late",
        summary="Выезд до 16:00, доплата 50% суток",
        amount=17_500,
        currency="KZT",
        closed=False,
        author="DEMO2",
        ago=260,
        changes=(
            _Entry(
                "DEMO1",
                140,
                "суть: «Выезд до 14:00, доплата 25% суток» → «Выезд до 16:00, доплата 50% суток»",
            ),
            _Entry("DEMO1", 140, f"сумма 8{_NBSP}750 → 17{_NBSP}500{_NBSP}₸"),
        ),
    ),
    _Row(
        key="r4",
        room="0623",
        kind="fine",
        summary="Курение в номере, акт составлен",
        amount=50_000,
        currency="KZT",
        closed=False,
        author="DEMO1",
        ago=180,
        notes=(_Entry("DEMO3", 150, "Гость спорит, ждём решения менеджера."),),
    ),
    _Row(
        key="r5",
        room="1110",
        kind="pay_later",
        summary="Компания оплатит по счёту",
        amount=142_000,
        currency="KZT",
        closed=True,
        author="DEMO3",
        ago=470,
        changes=(_Entry("DEMO2", 60, "статус: открыт → закрыт"),),
        notes=(_Entry("DEMO2", 61, "Оплата пришла, сверено с бухгалтерией."),),
    ),
    _Row(
        key="r6",
        room="0905",
        kind="deposit",
        summary="Залог за ключ от сейфа",
        amount=10_000,
        currency="KZT",
        closed=True,
        author="DEMO1",
        ago=380,
        changes=(_Entry("DEMO3", 35, "статус: открыт → закрыт"),),
    ),
    _Row(
        key="r7",
        room="1318",
        kind="other",
        summary="Забытая зарядка в камере хранения — гость заберёт",
        amount=None,
        currency="KZT",
        closed=False,
        author="DEMO2",
        ago=25,
    ),
    _Row(
        key="r8",
        room="0710/0712",
        kind="other",
        summary="Группа, организатор оплатит утром",
        amount=None,
        currency="KZT",
        closed=False,
        author="DEMO3",
        ago=50,
        notes=(_Entry("DEMO3", 48, "Телефон организатора — в карточке брони."),),
    ),
)


def _entry(entry: _Entry) -> dict[str, Any]:
    return {"login": entry.login, "ago": entry.ago, "text": entry.text}


def _row(row: _Row) -> dict[str, Any]:
    label = dict(KINDS)[row.kind]
    history = [_entry(_Entry(row.author, row.ago, CREATED)), *map(_entry, row.changes)]
    return {
        "key": row.key,
        "room": row.room,
        "kind": row.kind,
        "kind_label": label,
        "summary": row.summary,
        "amount": "" if row.amount is None else str(row.amount),
        "currency": row.currency,
        "money": money(row.amount, row.currency),
        "closed": row.closed,
        "author": row.author,
        "ago": row.ago,
        "history": history,
        "notes": [_entry(note) for note in row.notes],
    }


def _feed() -> list[dict[str, Any]]:
    """Лента «Изменения за смену»: все записи всех строк, свежие сверху."""
    feed: list[dict[str, Any]] = []
    for row in _ROWS:
        entries = [_Entry(row.author, row.ago, CREATED), *row.changes]
        feed += [{**_entry(e), "room": row.room, "row": row.key, "note": False} for e in entries]
        feed += [
            {**_entry(note), "room": row.room, "row": row.key, "note": True} for note in row.notes
        ]
    return sorted(feed, key=lambda item: item["ago"])


def open_total(rows: tuple[_Row, ...] = _ROWS) -> str:
    """Плитка «На сумму»: открытые хвосты по валютам — «87 500 ₸ · 300 $»."""
    parts = []
    for currency, _ in CURRENCIES:
        amounts = [r.amount for r in rows if not r.closed and r.currency == currency]
        total = sum(amount for amount in amounts if amount is not None)
        if total or currency == "KZT":
            parts.append(money(total, currency))
    return " · ".join(parts)


def build_board_context(staff: StaffContext) -> dict[str, Any]:
    """Контекст `_handover_board.html`: строки, лента, плитки и кто вошёл."""
    closed = sum(row.closed for row in _ROWS)
    return {
        # Правки вошедшего подписываются логином отеля (spec 0037), без логина —
        # именем: как в журнале изменений (spec 0041 §2.3).
        "me": staff.login or staff.display_name,
        "kinds": KINDS,
        "currencies": CURRENCIES,
        "rows": [_row(row) for row in _ROWS],
        "blank": {
            "key": "",
            "room": "",
            "kind": KINDS[0][0],
            "kind_label": KINDS[0][1],
            "summary": "",
            "amount": "",
            "currency": "KZT",
            "money": money(None, "KZT"),
            "closed": False,
            "author": "",
            "ago": 0,
            "history": [],
            "notes": [],
        },
        "feed": _feed(),
        "counts": {"all": len(_ROWS), "open": len(_ROWS) - closed, "closed": closed},
        "open_total": open_total(),
    }
