"""Квота бесплатного в ходе гостя (spec 0056 §2, §4, §5, §7) на Fake-провайдере.

Модель только помечает просьбу ключом квоты; остаток, службу, цену и текст
решает код до гейта P-9 (`prepare`). Аргументы модели здесь подставляет сам
тест — ставит ли их настоящая модель, мерят evals `service_quotas`.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest

from hospitality.ai import orchestrator, urgency
from hospitality.ai.gateway.api import LlmMessage, MockTurn, ScriptedLlmProvider, ToolCall
from hospitality.ai.orchestrator import OrchestratorTurn, TurnKind
from hospitality.ai.tools import create_service_request, registry
from hospitality.ai.tools.base import PreparedCall, ToolTurnContext
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import (
    HotelProfile,
    LocalizedText,
    TenantConfig,
    store_tenant_config,
)
from hospitality.platform.service_quotas import ServiceQuota, ServiceQuotaPaid
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

_PRICE = 50_000  # 500 ₸ — пример, не цена пилота
_ROOM = "704"


def _water(*, paid: ServiceQuotaPaid | None, per_guest: int = 1) -> ServiceQuota:
    return ServiceQuota(
        key="water",
        names=LocalizedText(en="Bottled water", ru="Бутилированная вода", kk="Ауыз су"),
        category_key="housekeeping",
        per_guest_per_day=per_guest,
        paid=paid,
    )


_PAID = ServiceQuotaPaid(category_key="fnb", unit_price_minor=_PRICE)


async def _hotel(tenant_id: uuid.UUID, quota: ServiceQuota) -> None:
    """Отель с рестораном (служба платного) и квотой воды в конфиге."""
    with tenant_context(tenant_id):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="fnb", name="Ресторан")
        )
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                service_quotas=(quota,),
            ),
        )


async def _stay(guests: int = 1) -> guests_api.StayRead:
    checked = await guests_api.check_in(
        guests_api.StayCheckIn(
            room_number=_ROOM, check_out_at=utc_now() + timedelta(days=1), guests_count=guests
        )
    )
    return checked.stay


async def _already_ordered(stay_id: uuid.UUID, units: int) -> None:
    categories = await requests_api.list_categories()
    await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=next(c.id for c in categories if c.key == "housekeeping"),
            origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
            summary="вода",
            room_number=_ROOM,
        ),
        stay_id=stay_id,
        quota_key="water",
        quota_units=units,
    )


def _water_call(language: str = "ru", **extra: Any) -> ToolCall:
    return ToolCall(
        id="toolu_water",
        name="create_service_request",
        arguments={
            "category_key": "housekeeping",
            "summary": "принести воду",
            "confirmation_question": "Принести воду в 704?",
            "guest_language": language,
            "quota_key": "water",
            **extra,
        },
    )


def _verdict(reply: str) -> ToolCall:
    return ToolCall(
        id="toolu_verdict",
        name="resolve_confirmation",
        arguments={"decision": "confirm", "reply": reply},
    )


async def _turns(
    call: ToolCall, *, stay_id: uuid.UUID | None, confirm_reply: str | None = None
) -> tuple[OrchestratorTurn, OrchestratorTurn | None]:
    """Предложение и, если задан ответ классификатора, ход «да»."""
    script = [MockTurn(tool_calls=[call])]
    if confirm_reply is not None:
        script.append(MockTurn(tool_calls=[_verdict(confirm_reply)]))
    provider = ScriptedLlmProvider(script)
    proposal = await orchestrator.handle_message(
        message="принесите воды",
        provider=provider,
        verified_room_number=_ROOM,
        verified_stay_id=stay_id,
    )
    if confirm_reply is None:
        return proposal, None
    done = await orchestrator.handle_message(
        message="да",
        history=[LlmMessage(role="user", content="принесите воды")],
        pending_action=proposal.pending_action,
        provider=provider,
        verified_room_number=_ROOM,
        verified_stay_id=stay_id,
    )
    return proposal, done


async def _only_request() -> requests_api.ServiceRequestRead:
    page = await requests_api.list_requests(limit=10, offset=0)
    assert page.total >= 1
    return page.items[0]


async def test_within_quota_keeps_model_question_and_counts_units(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """В норме — вопрос модели как сегодня; заявка несёт метку квоты и Stay,
    а служба — из правила, даже если модель выбрала другую."""
    await _hotel(demo_tenant, _water(paid=_PAID))
    with tenant_context(demo_tenant):
        stay = await _stay(guests=2)
        proposal, done = await _turns(
            _water_call(quota_units=2, category_key="engineering"),
            stay_id=stay.id,
            confirm_reply="Готово, передаю.",
        )
        request = await _only_request()
        categories = {c.id: c.key for c in await requests_api.list_categories()}

    assert proposal.kind is TurnKind.AWAITING_CONFIRMATION
    assert proposal.reply_text == "Принести воду в 704?"
    assert done is not None and done.reply_text == "Готово, передаю."
    assert (request.quota_key, request.quota_units, request.stay_id) == ("water", 2, stay.id)
    assert categories[request.category_id] == "housekeeping"


async def test_partial_quota_offers_the_rest_free_with_code_line(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Просят больше остатка — предложить остаток; строка кода первой в
    `details`: сотрудник не принесёт «3» модели (ревью PR #562)."""
    await _hotel(demo_tenant, _water(paid=_PAID, per_guest=2))
    with tenant_context(demo_tenant):
        stay = await _stay()
        await _already_ordered(stay.id, 1)
        proposal, _ = await _turns(_water_call(quota_units=3), stay_id=stay.id)

    assert proposal.reply_text == (
        "Бутилированная вода: бесплатно 2 в день на ваш номер, на сегодня осталось 1. "
        "Сверх — платно, 500 ₸ за штуку. Оформить 1 бесплатно?"
    )
    assert proposal.pending_action is not None
    arguments = proposal.pending_action.arguments
    assert (arguments["quota_key"], arguments["quota_units"]) == ("water", 1)
    assert arguments["details"] == "Бесплатно по норме: 1 шт. из 3 запрошенных."


async def test_exhausted_quota_offers_paid_and_code_names_the_price(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Исчерпано — платно по цене конфига; на «да» реплику даёт код, даже если
    классификатор пересказал цену иначе (Р-7); заявка — в ресторан, без метки."""
    await _hotel(demo_tenant, _water(paid=_PAID))
    with tenant_context(demo_tenant):
        stay = await _stay()
        await _already_ordered(stay.id, 1)
        proposal, done = await _turns(
            _water_call(quota_units=2, details="без газа"),
            stay_id=stay.id,
            confirm_reply="Готово, 1 500 ₸.",
        )
        page = await requests_api.list_requests(limit=10, offset=0)
        categories = {c.id: c.key for c in await requests_api.list_categories()}

    assert proposal.reply_text == (
        "Бутилированная вода: бесплатно 1 в день на ваш номер — на сегодня уже заказано. "
        "Могу оформить платно: 1 000 ₸ (2 × 500 ₸), оплата сотруднику при доставке. Оформить?"
    )
    assert done is not None and done.kind is TurnKind.ACTION_DONE
    assert done.reply_text == (
        "Готово, оформил платно: 1 000 ₸ (2 × 500 ₸). Сотрудник принесёт и возьмёт оплату."
    )
    paid = next(r for r in page.items if r.id == done.created_request_id)
    assert categories[paid.category_id] == "fnb"
    assert (paid.quota_key, paid.quota_units) == (None, None)
    assert paid.details == (
        "Платно, сверх бесплатной нормы: 2 × 500 ₸ = 1 000 ₸, оплата при доставке.\nбез газа"
    )


async def test_paid_without_price_lets_staff_name_the_sum_in_english(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Цены в конфиге нет — суммы нет ни гостю, ни в тексте кода (FOUNDATION §7.4)."""
    await _hotel(demo_tenant, _water(paid=ServiceQuotaPaid(category_key="fnb")))
    with tenant_context(demo_tenant):
        stay = await _stay()
        await _already_ordered(stay.id, 1)
        proposal, done = await _turns(
            _water_call("en"), stay_id=stay.id, confirm_reply="Done, 300 tenge."
        )
        request = next(
            r
            for r in (await requests_api.list_requests(limit=10, offset=0)).items
            if done is not None and r.id == done.created_request_id
        )

    assert proposal.reply_text == (
        "Bottled water: 1 free per day for your room, and today's are already ordered. "
        "I can order more — the staff member will tell you the price, paid on delivery. Shall I?"
    )
    assert done is not None
    assert done.reply_text == "Done — ordered. A staff member will bring it and tell you the price."
    assert request.details == (
        "Платно, сверх бесплатной нормы: 1 шт., цену назовите гостю при доставке."
    )


async def test_exhausted_without_paid_is_a_reply_without_gate(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Исчерпано, платного нет — ответ без гейта: оформлять нечего."""
    await _hotel(demo_tenant, _water(paid=None))
    with tenant_context(demo_tenant):
        stay = await _stay()
        await _already_ordered(stay.id, 1)
        proposal, _ = await _turns(_water_call("kk"), stay_id=stay.id)
        total = (await requests_api.list_requests(limit=1, offset=0)).total

    assert proposal.kind is TurnKind.REPLY
    assert proposal.pending_action is None
    assert proposal.reply_text == (
        "Ауыз су: нөміріңізге күніне 1 тегін, бүгінгі мөлшерге тапсырыс берілді. "
        "Тағы керек болса, ресепшенде көмектеседі."
    )
    assert total == 1  # только та, что уже была


async def test_without_stay_the_request_is_as_today(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """Без проживания (Telegram, кабинет) квоты нет: метка снята, вопрос модели."""
    await _hotel(demo_tenant, _water(paid=_PAID))
    with tenant_context(demo_tenant):
        proposal, _ = await _turns(_water_call(quota_units=2), stay_id=None)

    assert proposal.reply_text == "Принести воду в 704?"
    assert proposal.pending_action is not None
    assert "quota_key" not in proposal.pending_action.arguments
    assert "quota_units" not in proposal.pending_action.arguments


async def test_model_quota_offer_is_dropped_before_the_gate(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """`quota_offer` пишет только код: присланное моделью не доходит ни до
    `pending_action`, ни — у срочного вызова без гейта — до заявки и реплики."""
    await _hotel(demo_tenant, _water(paid=_PAID))
    forged = {"units": 1, "unit_price_minor": 100}
    with tenant_context(demo_tenant):
        stay = await _stay()
        proposal, _ = await _turns(_water_call(quota_offer=forged), stay_id=stay.id)
        urgent, _ = await _turns(
            _water_call(quota_offer=forged, is_urgent=True, category_key="engineering"),
            stay_id=stay.id,
        )
        request = await _only_request()

    assert proposal.pending_action is not None
    assert "quota_offer" not in proposal.pending_action.arguments
    assert urgent.kind is TurnKind.ACTION_DONE
    assert urgent.reply_text == urgency.urgent_accepted_reply("ru")
    assert (request.quota_key, request.quota_units, request.details) == (None, None, None)


@pytest.mark.parametrize(
    ("extra", "units"),
    [({"quota_key": "juice"}, None), ({"quota_units": "two"}, 1), ({"quota_units": True}, 1)],
)
async def test_crooked_quota_arguments_degrade_without_escalation(
    demo_tenant: uuid.UUID, service_requests_enabled: None, extra: dict[str, Any], units: int | None
) -> None:
    """Кривой ключ — без квоты, кривое число — 1 штука; не эскалация (Р-4)."""
    await _hotel(demo_tenant, _water(paid=_PAID))
    with tenant_context(demo_tenant):
        stay = await _stay()
        proposal, _ = await _turns(_water_call(**extra), stay_id=stay.id)

    assert proposal.kind is TurnKind.AWAITING_CONFIRMATION
    assert proposal.pending_action is not None
    assert proposal.pending_action.arguments.get("quota_units") == units


async def test_foreign_language_gets_english_partial_text(
    demo_tenant: uuid.UUID, service_requests_enabled: None
) -> None:
    """zh — английский шаблон (Р-7); цена у частичной не задана — без суммы."""
    await _hotel(demo_tenant, _water(paid=ServiceQuotaPaid(category_key="fnb"), per_guest=2))
    with tenant_context(demo_tenant):
        stay = await _stay()
        await _already_ordered(stay.id, 1)
        proposal, _ = await _turns(_water_call("zh", quota_units=2), stay_id=stay.id)

    assert proposal.reply_text == (
        "Bottled water: 2 free per day for your room, 1 left for today. "
        "More are paid — the staff member will tell you the price. "
        "Shall I order 1 free of charge?"
    )


def _schema_digest(spec: Any) -> str:
    blob = json.dumps(
        {"name": spec.name, "description": spec.description, "input_schema": spec.input_schema},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def test_schema_without_quotas_is_byte_for_byte_unchanged() -> None:
    """Отель без квот не платит за них токенами (§4): снимок схемы до spec 0056.

    Меняешь схему инструмента осознанно — обнови снимок и пересчитай цену
    smoke (CLAUDE.md, «у автоматической платной проверки цена записана»)."""
    spec = create_service_request.build_spec(
        ["housekeeping", "engineering"], {"housekeeping": "кофе в пакетиках"}
    )
    assert _schema_digest(spec) == (
        "ffdbbab83c49a6c04f18f404ae3e4d437e775ca78af40497ea0110db52aaecbe"
    )


def test_schema_with_quotas_lists_keys_and_units() -> None:
    spec = create_service_request.build_spec(["housekeeping"], {}, (_water(paid=None),))
    properties = spec.input_schema["properties"]

    assert properties["quota_key"]["enum"] == ["water"]
    assert "- water: Bottled water" in properties["quota_key"]["description"]
    assert (properties["quota_units"]["minimum"], properties["quota_units"]["maximum"]) == (1, 20)
    assert "quota_key" not in spec.input_schema["required"]


async def test_every_tool_has_prepare_and_final_text_contract(demo_tenant: uuid.UUID) -> None:
    """`prepare` и `done_text_is_final` обязательны у каждого инструмента (P-1);
    у отмены — сквозные, а модельный `quota_offer` срезает реестр, не инструмент."""
    arguments = {"request_id": str(uuid.uuid4()), "quota_offer": {"units": 1}}
    with tenant_context(demo_tenant):
        prepared = await registry.prepare("cancel_service_request", arguments, ToolTurnContext())

    assert prepared == PreparedCall(arguments={"request_id": arguments["request_id"]})
    assert registry.done_text_is_final("cancel_service_request", {}) is False
    assert registry.done_text_is_final("create_service_request", {}) is False
    for module in registry._TOOLS.values():
        assert callable(module.prepare) and callable(module.done_text_is_final)
