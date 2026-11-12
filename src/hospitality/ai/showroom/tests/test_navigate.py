"""Ход помощника витрины: правила, модель через шлюз, запасной путь (spec 0069 §7 п. 4).

Fake-адаптер шлюза вместо модели (ADR-007): что ответит настоящая модель, мерят
evals `ai/evals/showroom_navigate.py` с согласия основателя. Здесь — что ход
спрашивает у модели, что делает с её ответом и с отказом.
"""

# ruff: noqa: F811

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from hospitality.ai.evals.showroom_navigate import EVAL_SCENE
from hospitality.ai.gateway.api import MockLlmProvider, ToolCall
from hospitality.ai.showroom import navigate as navigate_module
from hospitality.ai.showroom.navigate import SHOWROOM_TOOLS, NavigationResult, navigate
from hospitality.ai.showroom.tests.conftest import (
    FakeRooms,
    fake_rooms,  # noqa: F401
    hotel,  # noqa: F401
    showroom_model,  # noqa: F401
)
from hospitality.ai.tools import navigate_showroom
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.ai.tools.navigate_showroom import Audience
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context

TODAY = date(2026, 11, 4)
RESTING_RU = "Помощник отдыхает — пользуйтесь кнопками этажей и видов."


def _model(**arguments: Any) -> MockLlmProvider:
    return MockLlmProvider(
        text="",
        tool_calls=[ToolCall(id="call-1", name=navigate_showroom.NAME, arguments=arguments)],
    )


async def _navigate(
    tenant_id: uuid.UUID,
    text: str,
    provider: MockLlmProvider,
    *,
    audience: Audience = "group",
    assistant_enabled: bool = True,
) -> NavigationResult:
    with tenant_context(tenant_id):
        return await navigate(
            text,
            scene=EVAL_SCENE,
            audience=audience,
            language="ru",
            today=TODAY,
            assistant_enabled=assistant_enabled,
            provider=provider,
        )


@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_short_command_is_answered_by_rules_without_the_model(hotel: uuid.UUID) -> None:
    provider = _model(commands=[], reply="")

    result = await _navigate(hotel, "этаж 15, вид на горы", provider)

    assert provider.calls == []
    assert result == NavigationResult(
        commands=[
            {"type": "focus_floor", "floor": 15},
            {"type": "set_view", "preset": "mountains"},
        ],
        reply="",
        source="rules",
    )


@pytest.mark.usefixtures("showroom_model")
async def test_phrase_goes_to_the_showroom_purpose_with_a_forced_tool(
    hotel: uuid.UUID, fake_rooms: FakeRooms
) -> None:
    provider = _model(
        commands=[
            {"type": "find_rooms", "floor_from": 20, "floor_to": 19, "side": "south", "count": 12},
            {"type": "set_view", "preset": "mountains"},
        ],
        reply="Показал 12 номеров на 19–20 этажах с горной стороны.",
    )

    result = await _navigate(hotel, "Высокий этаж с видом на горы, двенадцать номеров", provider)

    (request,) = provider.calls
    assert request.purpose == "showroom"
    assert request.forced_tool == navigate_showroom.NAME
    assert [tool.name for tool in request.tools] == [navigate_showroom.NAME]
    assert request.max_tokens == navigate_module.MAX_TOKENS
    assert request.system is not None
    assert "Today at the hotel is Wednesday, 4 November 2026." in request.system
    (query,) = fake_rooms.queries
    assert (query.floor_from, query.floor_to, query.side) == (19, 20, "south")
    found, view = result.commands
    assert found["type"] == "find_rooms"
    assert found["rooms"][:3] == ["2009", "2010", "2011"]
    assert len(found["rooms"]) == 12
    assert view == {"type": "set_view", "preset": "mountains"}
    assert result.reply == "Показал 12 номеров на 19–20 этажах с горной стороны."
    assert result.source == "model"


@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_rooms_on_floors_not_for_sale_are_never_offered(hotel: uuid.UUID) -> None:
    """Этаж 21 сцены — помесячный (`sellable: false`): подбор его номер не предлагает."""
    provider = _model(
        commands=[{"type": "find_rooms", "floor_from": 20, "type_key": "deluxe", "count": 3}],
        reply="Показал угловые номера наверху.",
    )

    (found,) = (await _navigate(hotel, "угловые номера повыше", provider)).commands

    assert found["rooms"] == ["2001", "2008", "2009"]


@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_commands_outside_the_scene_are_dropped_and_the_rest_run(
    hotel: uuid.UUID, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)  # запись — INFO; уровень, оставленный соседом, не в счёт
    provider = _model(
        commands=[
            {"type": "focus_floor", "floor": 40},
            {"type": "focus_room", "room": "4012"},
            {"type": "find_rooms", "view_key": "sea"},
            {"type": "focus_room", "room": "1509"},
        ],
        reply="Показал номер 1509.",
    )

    result = await _navigate(hotel, "покажи 4012 и 1509", provider)

    assert result.commands == [{"type": "focus_room", "room": "1509"}]
    assert caplog.text.count("showroom_command_dropped") == 3


@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_rule_with_a_floor_outside_the_scene_goes_to_the_model(hotel: uuid.UUID) -> None:
    provider = _model(commands=[{"type": "reset"}], reply="Такого этажа нет — показал здание.")

    result = await _navigate(hotel, "этаж 45", provider)

    assert len(provider.calls) == 1
    assert result.source == "model"


@pytest.mark.parametrize(
    "reply",
    [
        "Стандарт — от 45 000 ₸ за ночь.",
        "Около 120 $ за ночь.",
        "It costs 90 USD per night.",
        "Стоимость — 30 000 тенге.",
    ],
)
@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_reply_with_a_price_is_replaced(hotel: uuid.UUID, reply: str) -> None:
    result = await _navigate(hotel, "сколько стоит стандарт?", _model(commands=[], reply=reply))

    assert result.reply == "Подобрал варианты — смотрите на модели."


@pytest.mark.usefixtures("fake_rooms")
async def test_assistant_switched_off_in_config_never_calls_the_model(hotel: uuid.UUID) -> None:
    provider = _model(commands=[], reply="")

    result = await _navigate(hotel, "номера для группы", provider, assistant_enabled=False)

    assert provider.calls == []
    assert result == NavigationResult(commands=[], reply=RESTING_RU, source="fallback")


@pytest.mark.usefixtures("fake_rooms")
async def test_model_not_configured_is_the_fallback_not_an_error(hotel: uuid.UUID) -> None:
    """Пустая `LLM_MODEL_SHOWROOM` — ERR-AI-006 до провайдера: правила и кнопки."""
    provider = _model(commands=[], reply="")

    result = await _navigate(hotel, "номера для группы", provider)

    assert provider.calls == []
    assert result.source == "fallback"
    assert result.reply == RESTING_RU


@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_exhausted_budget_is_the_fallback_not_an_error(hotel: uuid.UUID) -> None:
    provider = _model(commands=[], reply="")

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("LLM_SHOWROOM_DAILY_BUDGET_USD", "0")
        get_settings.cache_clear()
        result = await _navigate(hotel, "номера для группы", provider)
    get_settings.cache_clear()

    assert provider.calls == []
    assert result.source == "fallback"


@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_answer_without_the_tool_is_the_fallback(hotel: uuid.UUID) -> None:
    result = await _navigate(hotel, "номера для группы", MockLlmProvider(text="Свободный текст"))

    assert result == NavigationResult(commands=[], reply=RESTING_RU, source="fallback")


@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_draft_request_only_fills_the_card_and_creates_nothing(hotel: uuid.UUID) -> None:
    provider = _model(
        commands=[
            {
                "type": "draft_request",
                "kind": "group",
                "guests": 2,
                "check_in": "2027-03-12",
                "check_out": "2027-03-15",
                "room_numbers": ["1509", "1510", "1511", "1512", "9999"],
                "view_key": "mountains",
                "type_key": "penthouse",
                "wishes": "тихий номер",
            }
        ],
        reply="Подготовил запрос на 12–15 марта для двоих — проверьте карточку.",
    )

    result = await _navigate(hotel, "с 12 по 15 марта, двое", provider, audience="booking")

    assert result.commands == [
        {
            "type": "draft_request",
            "kind": "booking",
            "guests": 2,
            "check_in": "2027-03-12",
            "check_out": "2027-03-15",
            "room_numbers": ["1509", "1510", "1511"],
            "view_key": "mountains",
            "type_key": None,
            "wishes": "тихий номер",
        }
    ]
    with tenant_context(hotel):
        async with session_scope() as session:
            assert await session.scalar(text("SELECT count(*) FROM service_requests")) == 0


@pytest.mark.parametrize(
    ("check_in", "check_out", "expected"),
    [
        ("2026-11-03", "2026-11-05", (None, None)),
        ("2027-03-12", "2027-03-12", ("2027-03-12", None)),
        ("2028-01-10", "2028-01-12", (None, None)),
        ("2026-11-04", "2026-11-05", ("2026-11-04", "2026-11-05")),
    ],
)
@pytest.mark.usefixtures("showroom_model", "fake_rooms")
async def test_draft_dates_the_form_would_refuse_are_left_for_the_visitor(
    hotel: uuid.UUID, check_in: str, check_out: str, expected: tuple[str | None, str | None]
) -> None:
    provider = _model(
        commands=[
            {"type": "draft_request", "kind": "group", "check_in": check_in, "check_out": check_out}
        ],
        reply="Проверьте карточку.",
    )

    (draft,) = (await _navigate(hotel, "даты", provider)).commands

    assert (draft["check_in"], draft["check_out"]) == expected


def test_language_reminder_closes_the_system_prompt_after_the_scene() -> None:
    """Правило языка — последним блоком, после сводки сцены (#812, evals PR #808)."""
    request = navigate_module.build_request(
        "我们需要城市一侧的18间房", scene=EVAL_SCENE, audience="group", today=TODAY
    )

    system = request.system or ""
    reminder = system.index("# Before you reply")
    assert navigate_module.SCENE_PLACEHOLDER not in system
    assert system.index("# This visit") < system.index("# Building") < reminder
    assert system.index("- 3–20: rooms") < reminder
    tail = system[reminder:]
    assert "\n# " not in tail
    assert "the visitor's message is the only thing that sets the language" in tail.lower()
    assert '"请显示七楼的房间" → "这是七楼。' in tail


def test_showroom_purpose_has_no_tool_with_a_side_effect() -> None:
    """Структурный инвариант P-9: запрос отправляет только кнопка посетителя."""
    assert SHOWROOM_TOOLS
    for tool in SHOWROOM_TOOLS:
        assert tool.CONFIRMATION_CLASS is ConfirmationClass.AUTO
        assert tool.CREATES_REQUEST is False
        assert not hasattr(tool, "execute")


def test_tool_schema_takes_views_types_and_floors_from_the_scene() -> None:
    spec = navigate_showroom.build_spec(
        floors_total=EVAL_SCENE.building.floors_total,
        view_keys=list(EVAL_SCENE.views),
        type_keys=list(EVAL_SCENE.room_types),
    )

    commands = spec.input_schema["properties"]["commands"]
    variants = {item["properties"]["type"]["const"]: item for item in commands["items"]["anyOf"]}
    find = variants["find_rooms"]["properties"]
    assert commands["maxItems"] == navigate_showroom.MAX_COMMANDS
    assert find["view_key"]["enum"] == ["mountains", "city"]
    assert find["type_key"]["enum"] == ["standard", "deluxe"]
    assert find["floor_from"]["maximum"] == 22
    assert set(variants) == {
        "focus_floor",
        "focus_room",
        "set_view",
        "step_floor",
        "reset",
        "find_rooms",
        "draft_request",
    }
