"""Данные демо-страницы «Забытые вещи» (issue #604) — к показу руководству 05.11.

Горничная записывает находку после выезда, ресепшен ищет её по номеру, дате
выезда и описанию сам, без переписки с хаускипингом. Находки выдуманы здесь;
поиск, форма «Найдена вещь» и смена статуса живут только в браузере
(`static/demo_lost_found.js`): ни БД, ни событий, ни загрузки файла — фото из
формы не покидает браузер, перезагрузка возвращает исходный список.

Даты выезда — смещением от сегодняшнего дня отеля: показ в любой день видит
свежие находки, а «за 7 дней» не пустеет. Людей нет: кто нашёл и кто выдал —
должности, номера — с этажей демо-отеля 13–25 (уточнение основателя к #603).
Документ и банковская карта — без картинки (NG-3 и ПД, разбор —
docs/DISCUSSION_LOG.md, «Забытые вещи»).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final, Literal

from hospitality.shared.humanize import month_day_ru

Status = Literal["stored", "claimed", "returned", "expired"]

STATUS_LABELS: Final[dict[Status, str]] = {
    "stored": "Хранится",
    "claimed": "Гость нашёлся — ждёт выдачи",
    "returned": "Выдано гостю",
    "expired": "Срок хранения истёк",
}
# Где хранится: (ключ — `value` выбора в форме, подпись). Документы и карты —
# только сейф ресепшена: переключатель формы выбирает его сам.
STORAGES: Final = (
    ("pantry", "Кладовая хаускипинга"),
    ("safe", "Сейф ресепшена"),
)
SAFE: Final = "safe"
# Поле `?room=` длиннее номера не бывает; хвост отрезается до шаблона.
ROOM_PREFILL_MAX: Final = 16


@dataclass(frozen=True)
class _Find:
    key: str
    title: str
    room: str
    days_ago: int  # выезд — столько дней назад от сегодняшнего дня отеля
    finder: str  # должность, без имён
    storage: str  # ключ из STORAGES
    status: Status
    art: str  # картинка-заглушка из `_lost_found_art.html`
    tone: int  # фон картинки, 1…4
    returned_days_ago: int | None = None  # только у «выдано гостю»
    returned_by: str = ""  # должность выдавшего, без ПД гостя
    no_photo: bool = False  # документ или карта — не фотографируют


_FINDS: Final[tuple[_Find, ...]] = (
    _Find(
        "f01",
        "Документ — паспорт",
        "2302",
        0,
        "горничная 23-го этажа",
        SAFE,
        "stored",
        "lock",
        4,
        no_photo=True,
    ),
    _Find(
        "f02",
        "Зарядка для телефона, белая",
        "1504",
        1,
        "горничная 15-го этажа",
        "pantry",
        "stored",
        "charger",
        1,
    ),
    _Find(
        "f03",
        "Наушники беспроводные в белом кейсе",
        "1903",
        1,
        "горничная 19-го этажа",
        SAFE,
        "stored",
        "headphones",
        2,
    ),
    _Find(
        "f04",
        "Очки в чёрном футляре",
        "2108",
        2,
        "горничная 21-го этажа",
        "pantry",
        "claimed",
        "glasses",
        3,
    ),
    _Find(
        "f05",
        "Банковская карта",
        "1607",
        2,
        "горничная 16-го этажа",
        SAFE,
        "claimed",
        "lock",
        4,
        no_photo=True,
    ),
    _Find(
        "f06",
        "Плюшевый заяц, серый",
        "1712",
        3,
        "горничная 17-го этажа",
        "pantry",
        "returned",
        "toy",
        1,
        returned_days_ago=1,
        returned_by="администратор ресепшена",
    ),
    _Find(
        "f07",
        "Часы наручные, металлический браслет",
        "2410",
        4,
        "старшая горничная 24-го этажа",
        SAFE,
        "returned",
        "watch",
        2,
        returned_days_ago=2,
        returned_by="менеджер ресепшена",
    ),
    _Find(
        "f08",
        "Шарф шерстяной, бордовый",
        "1311",
        5,
        "горничная 13-го этажа",
        "pantry",
        "stored",
        "scarf",
        3,
    ),
    _Find(
        "f09",
        "Зонт складной, синий",
        "1405",
        6,
        "горничная 14-го этажа",
        "pantry",
        "stored",
        "umbrella",
        1,
    ),
    _Find(
        "f10",
        "Книга в мягкой обложке",
        "2005",
        9,
        "горничная 20-го этажа",
        "pantry",
        "stored",
        "book",
        2,
    ),
    _Find(
        "f11",
        "Косметичка розовая",
        "1809",
        35,
        "горничная 18-го этажа",
        "pantry",
        "expired",
        "cosmetics",
        3,
    ),
    _Find(
        "f12",
        "Термос зелёный",
        "2512",
        40,
        "горничная 25-го этажа",
        "pantry",
        "expired",
        "thermos",
        1,
    ),
)


def _short(day: date) -> str:
    """«03.11» — дата выдачи в статусе, как в журнале ресепшена."""
    return f"{day.day:02d}.{day.month:02d}"


def _storage_label(key: str) -> str:
    return dict(STORAGES)[key]


def _card(find: _Find, today: date) -> dict[str, Any]:
    checkout = today - timedelta(days=find.days_ago)
    status_label = STATUS_LABELS[find.status]
    if find.returned_days_ago is not None:
        status_label = f"{status_label} {_short(today - timedelta(days=find.returned_days_ago))}"
    return {
        "key": find.key,
        "title": find.title,
        "room": find.room,
        "checkout": checkout.isoformat(),
        "checkout_label": month_day_ru(checkout),
        "finder": find.finder,
        "storage": _storage_label(find.storage).lower(),
        "status": find.status,
        "status_label": status_label,
        "returned_by": find.returned_by,
        "art": find.art,
        "tone": find.tone,
        "no_photo": find.no_photo,
        # Поиск «что ищем» — по описанию в нижнем регистре, ё как е.
        "search_text": find.title.lower().replace("ё", "е"),
    }


def room_prefill(raw: str | None) -> str:
    """Номер из `?room=` (ссылка с демо «Номера», #603) для поля формы: обрезан,
    в шаблон попадает только через автоэкранирование Jinja."""
    return (raw or "").strip()[:ROOM_PREFILL_MAX]


def build_demo_lost_found_context(today: date, room: str | None) -> dict[str, Any]:
    """Контекст шаблона `demo_lost_found.html`: находки, сегодняшний день отеля
    (умолчание даты выезда в форме и граница «за 7 дней») и номер из ссылки —
    с ним форма «Найдена вещь» открыта сразу."""
    prefill = room_prefill(room)
    return {
        "finds": [_card(find, today) for find in _FINDS],
        "today": today.isoformat(),
        "storages": STORAGES,
        "safe_storage": SAFE,
        "room_prefill": prefill,
        "open_form": bool(prefill),
    }
