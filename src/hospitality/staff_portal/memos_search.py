"""Поиск и фильтры реестра записок (spec 0077 §5; #936, PR 4б-3).

Ищет в приложении по строкам уже отобранной вкладки (`memos_api.list_memos`): заголовок
в базе — шифртекст (§8), поэтому SQL-поиска по нему нет, а записей с участием одного
сотрудника — десятки, не тысячи. Условия — параметры GET формы реестра: номер или
заголовок (`q`), часть имени участника (`who`), вид, статус, дата «с» и «по» — та же дата,
что в строке реестра (отправки, у черновика — создания), по поясу отеля. Неизвестное
значение условия — как пустое: ручная правка адреса ошибкой не отвечает. Текст условий
в логи не идёт — в нём имена и заголовки.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, tzinfo
from enum import StrEnum
from typing import Any, Final

from hospitality.modules.memos import api as memos_api
from hospitality.staff_portal import memos_texts as texts

# Длиннее заголовка (200 знаков, модуль) искать нечего.
QUERY_MAX_CHARS: Final = memos_api.TITLE_MAX_CHARS


@dataclass(frozen=True)
class MemoFilter:
    text: str = ""
    who: str = ""
    kind: memos_api.MemoKind | None = None
    status: memos_api.MemoStatus | None = None
    since: date | None = None
    until: date | None = None

    @property
    def active(self) -> bool:
        return self != MemoFilter()


def _choice[E: StrEnum](enum_type: type[E], value: str | None) -> E | None:
    try:
        return enum_type(value) if value else None
    except ValueError:
        return None


def _day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def parse_filter(params: Mapping[str, str]) -> MemoFilter:
    """Условия из параметров запроса страницы реестра."""
    return MemoFilter(
        text=params.get("q", "").strip()[:QUERY_MAX_CHARS],
        who=params.get("who", "").strip()[:QUERY_MAX_CHARS],
        kind=_choice(memos_api.MemoKind, params.get("kind")),
        status=_choice(memos_api.MemoStatus, params.get("status")),
        since=_day(params.get("since")),
        until=_day(params.get("until")),
    )


def matches(
    memo: memos_api.MemoSummary,
    condition: MemoFilter,
    name: Callable[[uuid.UUID], str],
    zone: tzinfo,
) -> bool:
    """Строка реестра подходит под все заданные условия; `name` — имя участника по id."""
    if condition.kind is not None and memo.kind is not condition.kind:
        return False
    if condition.status is not None and memo.status is not condition.status:
        return False
    day = (memo.sent_at or memo.created_at).astimezone(zone).date()
    if (condition.since is not None and day < condition.since) or (
        condition.until is not None and day > condition.until
    ):
        return False
    if condition.text:
        needle = condition.text.casefold()
        if needle not in (memo.number or "").casefold() and needle not in memo.title.casefold():
            return False
    if condition.who:
        needle = condition.who.casefold()
        people = (memo.author_id, memo.addressee_id, *memo.signer_ids)
        if not any(needle in name(user_id).casefold() for user_id in people):
            return False
    return True


def form_context(condition: MemoFilter, action: str) -> dict[str, Any]:
    """Форма поиска (`memo_list.html`): заданные значения и варианты выбора."""
    return {
        "search": {
            "action": action,
            "title": texts.SEARCH_TITLE,
            "hint": texts.SEARCH_HINT,
            "any": texts.SEARCH_ANY,
            "active": condition.active,
            "q": condition.text,
            "who": condition.who,
            "kind": condition.kind.value if condition.kind else "",
            "status": condition.status.value if condition.status else "",
            "since": condition.since.isoformat() if condition.since else "",
            "until": condition.until.isoformat() if condition.until else "",
            "max_chars": QUERY_MAX_CHARS,
            "kinds": [{"value": kind.value, "title": title} for kind, title in texts.KIND.items()],
            "statuses": [
                {"value": status.value, "title": title} for status, title in texts.STATUS.items()
            ],
        }
    }
