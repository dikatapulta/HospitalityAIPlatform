"""Фильтры страницы очереди: вкладка, служба, «мои», поиск (spec 0033 §5, #221).

Чистые функции без БД: разбор query-параметров, адрес страницы с фильтрами и
тексты пустой ленты. Чтение заявок — `queue.py`; отдельный файл — чтобы
страница очереди с четвёртым фильтром осталась в пределе R-3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import urlencode

QUEUE_TAB_OPEN: Final = "open"
QUEUE_TAB_CLOSED: Final = "closed"
QUEUE_TAB_WEEK: Final = "week"
_TABS: Final = frozenset({QUEUE_TAB_OPEN, QUEUE_TAB_CLOSED, QUEUE_TAB_WEEK})

# «За 7 дней» — сегодня и шесть дней до него, границы — полночи отеля.
WEEK_DAYS: Final = 7

# Длиннее колонки `room_number` комнаты не бывает — и в поле, и в URL.
_MAX_SEARCH_LENGTH: Final = 20
# Потолок дневного номера — у поиска и у плашки «заявка создана» (`queue.py`):
# тот же, которым команды персонала в Telegram отсекают вставленный кусок
# номера карты (issue #203). Число здесь своё, а не импортированное:
# `channels/telegram` — сиблинг композиционного слоя, кабинету его импортировать
# нельзя (контракт 7).
MAX_DAILY_NUMBER_DIGITS: Final = 5
_NUMBER_PREFIXES: Final = ("#", "№")

# Где искали — для текста «ничего не нашлось».
_TAB_SCOPE: Final = {
    QUEUE_TAB_OPEN: "Среди открытых",
    QUEUE_TAB_CLOSED: "Среди закрытых сегодня",
    QUEUE_TAB_WEEK: "За 7 дней",
}


@dataclass(frozen=True, slots=True)
class QueueSearch:
    """Строка поиска: `text` — как набрал сотрудник (для поля и текстов),
    ровно одно из `room_number` / `daily_number` — то, что ищется в SQL."""

    text: str = ""
    room_number: str | None = None
    daily_number: int | None = None


def parse_queue_tab(raw: str | None) -> str:
    """Вкладка из query-параметра; всё непонятное — «открытые» (не 422: ссылку
    с опечаткой мог прислать коллега, страница должна открыться)."""
    return raw if raw is not None and raw in _TABS else QUEUE_TAB_OPEN


def parse_search(raw: str | None) -> QueueSearch:
    """Поиск из параметра `q`: `#12` / `№12` — дневной номер, остальное — комната.

    Пустая строка — поиска нет. Непонятное (`#абв`) ищется как комната и честно
    ничего не находит — 422 на ссылку с опечаткой странице очереди не положен
    (канон `parse_queue_tab`).
    """
    text = (raw or "").strip()[:_MAX_SEARCH_LENGTH]
    if not text:
        return QueueSearch()
    digits = text[1:].strip()
    if (
        text.startswith(_NUMBER_PREFIXES)
        and digits.isdecimal()
        and len(digits) <= MAX_DAILY_NUMBER_DIGITS
    ):
        return QueueSearch(text=text, daily_number=int(digits))
    return QueueSearch(text=text, room_number=text)


def queue_params(
    *, tab: str, category_key: str | None, mine: bool, search: str = ""
) -> dict[str, str]:
    """Query-параметры страницы — без значений по умолчанию, чтобы URL был короче."""
    params: dict[str, str] = {}
    if tab != QUEUE_TAB_OPEN:
        params["tab"] = tab
    if category_key:
        params["category"] = category_key
    if mine:
        params["mine"] = "1"
    if search:
        params["q"] = search
    return params


def queue_path(
    tenant_slug: str, *, tab: str, category_key: str | None, mine: bool, search: str = ""
) -> str:
    """URL страницы очереди с фильтрами — чипсы-ссылки и fragment-эндпоинт."""
    params = queue_params(tab=tab, category_key=category_key, mine=mine, search=search)
    query = f"?{urlencode(params)}" if params else ""
    return f"/staff/{tenant_slug}/requests{query}"


def empty_text(tab: str, search: QueueSearch) -> str:
    """Текст пустой ленты: без поиска — по вкладке, с поиском — что и где искали."""
    if search.daily_number is not None:
        return f"{_TAB_SCOPE[tab]} заявки #{search.daily_number} нет."
    if search.text:
        return f"{_TAB_SCOPE[tab]} заявок по комнате {search.text} нет."
    if tab == QUEUE_TAB_CLOSED:
        return "Сегодня ещё ничего не закрывали."
    if tab == QUEUE_TAB_WEEK:
        return "За 7 дней заявок не было."
    return "Открытых заявок нет — отличная работа!"


def number_hint(search: QueueSearch) -> str | None:
    """Искали комнату одними цифрами и не нашли — может, это был номер заявки."""
    room = search.room_number
    if room is None or not room.isdecimal() or len(room) > MAX_DAILY_NUMBER_DIGITS:
        return None
    return f"Номер заявки ищите с решёткой: #{room}."
