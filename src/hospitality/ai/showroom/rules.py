"""Правила без модели: короткая команда посетителя → команды камеры (spec 0069 §2.3 п. 1).

«этаж 15», «room 1507», «горы», «с улицы», «выше», «сначала» — на ru, en, kk. Правило
срабатывает, только когда фраза ЦЕЛИКОМ состоит из команд и слов-связок: «высокий
этаж с видом на горы, двенадцать номеров» тоже содержит «горы», но смысл фразы —
подбор номеров, и угаданный по одному слову вид был бы неправдой. Осталось
незнакомое слово — решает модель.

Это и запасной путь: модель выключена или бюджет исчерпан — правила и кнопки.
Этаж и номер правила не проверяют — сцену знает `navigate.py`.
"""

from __future__ import annotations

import re
from typing import Final

from hospitality.ai.tools.navigate_showroom import (
    Command,
    FocusFloor,
    FocusRoom,
    Reset,
    SetView,
    StepFloor,
)

MAX_RULE_TEXT_LENGTH: Final = 60

_FLOOR_WORD: Final = r"(?:этаж\w*|floor|қабат\w*)"
_ROOM_WORD: Final = r"(?:номер\w*|room|бөлме\w*|нөмір\w*)"

# Порядок важен: «жоғарыдан» (сверху) раньше «жоғары» (выше), этаж и номер — раньше
# всего, чтобы цифра не осталась «лишним словом».
_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(pattern), kind)
    for pattern, kind in (
        (rf"\b{_FLOOR_WORD}\s+(\d{{1,2}})\b", "floor"),
        (rf"\b(\d{{1,2}})(?:\s*(?:й|ой|ый|ий|ші|шы|th|st|nd|rd))?\s+{_FLOOR_WORD}", "floor"),
        (rf"\b{_ROOM_WORD}\s+(\d{{2,5}})\b", "room"),
        (rf"\b(\d{{3,5}})\s+{_ROOM_WORD}", "room"),
        (r"\b(?:сверху|top|from above|жоғарыдан)\b", "top"),
        (r"\b(?:с улицы|улиц[аыуе]|street|көшеден|көше(?:ге|ні)?)\b", "street"),
        (r"\b(?:горы|гор|горам|горный|mountains?|тау(?:лар|ға|ды|дың|ларға|лары)?)\b", "mountains"),
        (r"\b(?:город(?:а|у|ом)?|city|қала(?:ға|ны|ның)?)\b", "city"),
        (r"\b(?:сначала|заново|reset|start over|басынан|қайтадан)\b", "reset"),
        (r"\b(?:выше|higher|up|жоғары(?:рақ)?)\b", "up"),
        (r"\b(?:ниже|lower|down|төмен(?:ірек)?)\b", "down"),
    )
)

# Слова-связки: без них правило не сработало бы на «покажи вид на горы».
_FILLER: Final = frozenset(
    {
        # ru
        "покажи",
        "покажите",
        "показать",
        "давай",
        "давайте",
        "вид",
        "виды",
        "видом",
        "на",
        "с",
        "со",
        "в",
        "и",
        "мне",
        "пожалуйста",
        "посмотреть",
        "где",
        "ещё",
        "еще",
        "этаж",
        "этажом",
        "этажа",
        "вернись",
        "вернуться",
        "к",
        "туда",
        # en
        "show",
        "me",
        "the",
        "a",
        "view",
        "views",
        "of",
        "to",
        "please",
        "go",
        "look",
        "at",
        "on",
        "with",
        "and",
        "back",
        "side",
        "where",
        "is",
        "one",
        "floor",
        # kk
        "көрсет",
        "көрсетіңіз",
        "көрсетші",
        "көрініс",
        "көрінісі",
        "жақ",
        "жағы",
        "жағын",
        "маған",
        "өтінемін",
        "бір",
        "қабат",
        "және",
    }
)

_PUNCTUATION: Final = re.compile(r"[^\w\s]+")
_SPACES: Final = re.compile(r"\s+")


def match_rules(text: str) -> list[Command] | None:
    """Команды, если фраза целиком из команд и связок; иначе None — решает модель."""
    normalized = _normalize(text)
    if not normalized or len(normalized) > MAX_RULE_TEXT_LENGTH:
        return None
    found: list[tuple[int, Command]] = []
    rest = normalized
    for pattern, kind in _PATTERNS:
        for match in pattern.finditer(rest):
            found.append((match.start(), _command(kind, match)))
        rest = pattern.sub(lambda match: " " * len(match.group(0)), rest)
    if not found or any(word not in _FILLER for word in rest.split()):
        return None
    return [command for _, command in sorted(found, key=lambda item: item[0])]


def _normalize(text: str) -> str:
    return _SPACES.sub(" ", _PUNCTUATION.sub(" ", text.lower().replace("ё", "е"))).strip()


def _command(kind: str, match: re.Match[str]) -> Command:
    match kind:
        case "floor":
            return FocusFloor(floor=max(1, min(60, int(match.group(1)))))
        case "room":
            return FocusRoom(room=match.group(1))
        case "up" | "down":
            return StepFloor(direction=kind)
        case "reset":
            return Reset()
        case "mountains" | "city" | "top" | "street":
            return SetView(preset=kind)
        case _:
            raise AssertionError(f"unknown rule kind {kind!r}")
