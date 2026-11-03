"""Помощник 3D-витрины: фраза посетителя → команды камеры (spec 0069 §2.3).

Порядок хода: правила без модели (`rules.py`) → модель через шлюз инструментом
`navigate_showroom` (назначение `showroom`, своя модель и бюджет, ADR-024) →
проверка каждой команды по сцене. Модель выключена, бюджет исчерпан или шлюз
отказал (ERR-AI-001/002/003/006) — не ошибка, а запасной путь: правила и кнопки,
текст «Помощник отдыхает…» (§5).

Фраза посетителя — недоверенный ввод (§7.6): модель отвечает только аргументами
инструмента, и каждая команда проверяется кодом. Этаж, номер, вид или тип номера
не из сцены — команда отбрасывается (`showroom_command_dropped`), остальные идут.
`reply` с ценой заменяется статическим текстом: неправда посетителю дороже
пустой реплики. Фраза не хранится и в лог не пишется (§2.3 п. 5).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from types import ModuleType
from typing import Any, Final, Literal

from pydantic import TypeAdapter, ValidationError

from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import (
    ERR_AI_BUDGET_EXCEEDED,
    ERR_AI_PROVIDER_ERROR,
    ERR_AI_PROVIDER_TIMEOUT,
    ERR_AI_PURPOSE_NOT_CONFIGURED,
    LlmMessage,
    LlmProvider,
    LlmRequest,
)
from hospitality.ai.prompts import load_prompt
from hospitality.ai.showroom.rules import match_rules
from hospitality.ai.showroom.summary import render_summary
from hospitality.ai.tools import navigate_showroom
from hospitality.ai.tools.navigate_showroom import (
    MAX_FOUND_ROOMS,
    MAX_REPLY_LENGTH,
    Audience,
    Command,
    DraftRequest,
    FindRooms,
    FocusFloor,
    FocusRoom,
)
from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.showroom.api import SceneDocument
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_showroom_navigation

logger = get_logger(module=__name__)

PROMPT_NAME: Final = "showroom_navigate_v1"
MAX_TOKENS: Final = 300
MAX_TEXT_LENGTH: Final = 300
# Запрос гостя — не дальше 400 дней (§2.4): дальше черновик дат не заполняет.
MAX_DAYS_AHEAD: Final = 400
MAX_BOOKING_ROOMS: Final = 3

# Инструменты назначения `showroom` — единственный их список. Все без побочных
# эффектов (P-9): структурный инвариант стережёт тест, а не текст промпта.
SHOWROOM_TOOLS: Final[tuple[ModuleType, ...]] = (navigate_showroom,)

Source = Literal["rules", "model", "fallback"]

_FALLBACK_CODES: Final = frozenset(
    {
        ERR_AI_PROVIDER_TIMEOUT,
        ERR_AI_BUDGET_EXCEEDED,
        ERR_AI_PROVIDER_ERROR,
        ERR_AI_PURPOSE_NOT_CONFIGURED,
    }
)
# Запасной путь — тот же текст показывает страница, когда сеть или лимит не дали ответа.
RESTING_TEXTS: Final = {
    "en": "The assistant is resting — use the floor and view buttons.",
    "ru": "Помощник отдыхает — пользуйтесь кнопками этажей и видов.",
    "kk": "Көмекші демалып жатыр — қабат пен көрініс батырмаларын пайдаланыңыз.",
}
_PRICE_FREE_REPLY: Final = {
    "en": "I've picked some options — see them on the model.",
    "ru": "Подобрал варианты — смотрите на модели.",
    "kk": "Нұсқаларды таңдадым — модельден қараңыз.",
}
_PRICE: Final = re.compile(
    r"[₸$€£]|\b(?:usd|eur|kzt)\b|\d[\d\s.,]*\s*(?:тг|тенге|теңге|tenge|долл\w*|евро|dollars?|euros?)\b",
    re.IGNORECASE,
)
_COMMANDS: Final = TypeAdapter[Command](Command)


@dataclass(frozen=True)
class NavigationResult:
    """Ответ странице: команды в форме JSON, реплика под моделью, источник."""

    commands: list[dict[str, Any]]
    reply: str
    source: Source


async def navigate(
    text: str,
    *,
    scene: SceneDocument,
    audience: Audience,
    language: str,
    today: date,
    assistant_enabled: bool,
    provider: LlmProvider | None = None,
) -> NavigationResult:
    """Ход помощника (внутри `tenant_context`): правила, иначе модель, иначе запасной путь.

    `today` — дата в поясе отеля (FOUNDATION §9), `language` — язык страницы:
    на нём статические тексты. `provider` переопределяют тесты и evals.
    """
    ruled = match_rules(text)
    if ruled is not None:
        commands = await _resolve(ruled, scene, audience=audience, today=today)
        if commands:
            return _finish(commands, "", "rules")
    if not assistant_enabled:
        return _resting(language)
    try:
        response = await gateway.complete(
            build_request(text, scene=scene, audience=audience, today=today), provider=provider
        )
    except AppError as error:
        if error.code not in _FALLBACK_CODES:
            raise
        logger.warning("showroom_model_unavailable", error_code=error.code)
        return _resting(language)
    call = next((call for call in response.tool_calls if call.name == navigate_showroom.NAME), None)
    if call is None:
        logger.warning("showroom_no_tool_call")
        return _resting(language)
    commands = await _resolve(
        _parse(call.arguments.get("commands")), scene, audience=audience, today=today
    )
    return _finish(commands, _safe_reply(call.arguments.get("reply"), language), "model")


def build_request(
    text: str, *, scene: SceneDocument, audience: Audience, today: date
) -> LlmRequest:
    """Запрос к модели — один на ход страницы и на evals (§7 п. 8)."""
    return LlmRequest(
        system=load_prompt(PROMPT_NAME) + render_summary(scene, audience=audience, today=today),
        messages=[LlmMessage(role="user", content=text[:MAX_TEXT_LENGTH])],
        tools=[
            navigate_showroom.build_spec(
                floors_total=scene.building.floors_total,
                view_keys=list(scene.views),
                type_keys=list(scene.room_types),
            )
        ],
        forced_tool=navigate_showroom.NAME,
        max_tokens=MAX_TOKENS,
        purpose="showroom",
    )


def _parse(raw: object) -> list[Command]:
    """Команды модели по одной: кривая отбрасывается, остальные идут."""
    if not isinstance(raw, list):
        return []
    commands: list[Command] = []
    for item in raw[: navigate_showroom.MAX_COMMANDS]:
        try:
            commands.append(_COMMANDS.validate_python(item))
        except ValidationError:
            kind = item.get("type") if isinstance(item, dict) else None
            _dropped(str(kind), "invalid")
    return commands


async def _resolve(
    commands: Sequence[Command], scene: SceneDocument, *, audience: Audience, today: date
) -> list[dict[str, Any]]:
    """Сверить команды со сценой; `find_rooms` — исполнить сервисом ядра (P-5)."""
    floors = {floor.number for floor in scene.floors}
    rooms = {room.number for floor in scene.floors for room in floor.rooms}
    resolved: list[dict[str, Any]] = []
    for command in commands:
        if isinstance(command, FocusFloor) and command.floor not in floors:
            _dropped(command.type, "floor_not_in_scene")
        elif isinstance(command, FocusRoom) and command.room not in rooms:
            _dropped(command.type, "room_not_in_scene")
        elif isinstance(command, FindRooms):
            if found := await _find_rooms(command, scene):
                resolved.append(found)
        elif isinstance(command, DraftRequest):
            resolved.append(_draft(command, scene, rooms, audience=audience, today=today))
        else:
            resolved.append(command.model_dump(mode="json"))
    return resolved


async def _find_rooms(command: FindRooms, scene: SceneDocument) -> dict[str, Any] | None:
    if command.view_key is not None and command.view_key not in scene.views:
        _dropped(command.type, "view_not_in_scene")
        return None
    if command.type_key is not None and command.type_key not in scene.room_types:
        _dropped(command.type, "type_not_in_scene")
        return None
    low, high = command.floor_from, command.floor_to
    if low is not None and high is not None and low > high:
        low, high = high, low
    matches = await showroom_api.find_rooms(
        showroom_api.RoomQuery(
            floor_from=low,
            floor_to=high,
            side=command.side,
            view_key=command.view_key,
            type_key=command.type_key,
            count=command.count or MAX_FOUND_ROOMS,
        )
    )
    return {**command.model_dump(mode="json"), "rooms": [match.number for match in matches]}


def _draft(
    command: DraftRequest,
    scene: SceneDocument,
    rooms: set[str],
    *,
    audience: Audience,
    today: date,
) -> dict[str, Any]:
    """Черновик для карточки: только то, что пройдёт форму; остальное посетитель впишет."""
    numbers = [number for number in command.room_numbers if number in rooms]
    if len(numbers) != len(command.room_numbers):
        _dropped(command.type, "room_not_in_scene")
    check_in, check_out = command.check_in, command.check_out
    if check_in is not None and not today <= check_in <= today + timedelta(days=MAX_DAYS_AHEAD):
        _dropped(command.type, "check_in_out_of_range")
        check_in = check_out = None
    if check_out is not None and (check_in is None or check_out <= check_in):
        check_out = None
    draft = command.model_copy(
        update={
            # Вкладка страницы важнее догадки модели: форма — та, на которой посетитель.
            "kind": audience,
            "room_numbers": tuple(numbers[: MAX_BOOKING_ROOMS if audience == "booking" else None]),
            "check_in": check_in,
            "check_out": check_out,
            "view_key": command.view_key if command.view_key in scene.views else None,
            "type_key": command.type_key if command.type_key in scene.room_types else None,
        }
    )
    return draft.model_dump(mode="json")


def _safe_reply(raw: object, language: str) -> str:
    reply = raw.strip()[:MAX_REPLY_LENGTH] if isinstance(raw, str) else ""
    if _PRICE.search(reply):
        logger.warning("showroom_reply_price_replaced")
        return _text(_PRICE_FREE_REPLY, language)
    return reply


def _finish(commands: list[dict[str, Any]], reply: str, source: Source) -> NavigationResult:
    record_showroom_navigation(source)
    logger.info(
        "showroom_navigated", source=source, commands=[command["type"] for command in commands]
    )
    return NavigationResult(commands=commands, reply=reply, source=source)


def _resting(language: str) -> NavigationResult:
    return _finish([], _text(RESTING_TEXTS, language), "fallback")


def _dropped(kind: str, reason: str) -> None:
    logger.info("showroom_command_dropped", command=kind, reason=reason)


def _text(texts: dict[str, str], language: str) -> str:
    return texts.get(language, texts["en"])
