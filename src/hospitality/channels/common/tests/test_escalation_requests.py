"""Эскалация заводит заявку (spec 0039 §2, issue #292) — тесты §10.

Ход гостя зовётся напрямую (`run_guest_turn`), как его зовут оба канала. Исход
оркестратора подменяется: здесь проверяется, что канал делает с эскалацией, а
откуда эскалация берётся у модели, — `ai/tests` и `channels/telegram/tests`.
ЧП-перехват идёт настоящим путём: до модели он не доходит и так.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hospitality.ai import orchestrator, urgency
from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.ai.orchestrator import OrchestratorTurn, TurnKind
from hospitality.channels.common import escalation_request
from hospitality.channels.common.guest_turn import DEGRADED_REPLY, run_guest_turn
from hospitality.channels.common.models import ConversationEscalation
from hospitality.channels.common.store import ensure_conversation, load_conversation_request_ids
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis
from tests.test_logging import find_record, read_log_records

ROOM = "305"
NEEDS_HUMAN_REPLY = "Секунду, я подключу сотрудника отеля."


async def _configure(tenant_id: uuid.UUID, escalation_category_key: str | None) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                escalation_category_key=escalation_category_key,
            ),
        )


@pytest.fixture
async def hotel(
    demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """Отель пилота: служба `reception` задана службой эскалаций; веб-диалог гостя."""
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    with tenant_context(demo_tenant):
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="reception", name="Ресепшен")
        )
        conversation_id = await ensure_conversation("web", "stay-1")
    await _configure(demo_tenant, "reception")
    yield demo_tenant, conversation_id


def _llm_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(**_: Any) -> OrchestratorTurn:
        raise AppError(code="ERR-AI-001", message="provider timeout", status_code=503)

    monkeypatch.setattr(orchestrator, "handle_message", fail)


def _needs_human(
    monkeypatch: pytest.MonkeyPatch,
    reason: EscalationReason,
    *,
    action_summary: str | None = None,
    room_number: str | None = None,
) -> None:
    async def turn(**_: Any) -> OrchestratorTurn:
        return OrchestratorTurn(
            kind=TurnKind.NEEDS_HUMAN,
            reply_text=NEEDS_HUMAN_REPLY,
            escalation=EscalationContext(
                reason=reason,
                error_code="ERR-AI-004",
                tool_name="create_service_request",
                action_summary=action_summary,
                room_number=room_number,
            ),
        )

    monkeypatch.setattr(orchestrator, "handle_message", turn)


async def _guest_says(
    tenant_id: uuid.UUID, conversation_id: uuid.UUID, text: str, *, room: str | None = ROOM
) -> list[str]:
    replies: list[str] = []

    async def reply(reply_text: str) -> None:
        replies.append(reply_text)

    with tenant_context(tenant_id):
        await run_guest_turn(
            conversation_id,
            text,
            uuid.uuid4(),
            external_id="stay-1",
            rate_limit_key="stay-1",
            reply=reply,
            provider=None,
            verified_room_number=room,
        )
    return replies


async def _requests(tenant_id: uuid.UUID) -> list[requests_api.ServiceRequestRead]:
    with tenant_context(tenant_id):
        page = await requests_api.list_requests(limit=50, offset=0)
    return sorted(page.items, key=lambda item: item.created_at)


async def _escalation_rows(tenant_id: uuid.UUID) -> list[ConversationEscalation]:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            rows = await session.scalars(
                select(ConversationEscalation).order_by(ConversationEscalation.created_at)
            )
            return list(rows.all())


async def _set_status(
    tenant_id: uuid.UUID, request_id: uuid.UUID, *statuses: requests_api.RequestStatus
) -> None:
    with tenant_context(tenant_id):
        for status in statuses:
            await requests_api.change_request_status(request_id, status)


def test_card_limits_match_request_schema() -> None:
    """Пределы обрезки карточки — копия пределов схемы заявки. Разойдутся — схема
    отвергнет карточку целиком (`cause=unexpected`), и у отеля без бота пропадёт
    сигнал; поэтому расхождение ловит тест, а не дежурный."""
    fields = requests_api.ServiceRequestCreate.model_fields
    limits = {
        name: next(item.max_length for item in fields[name].metadata if hasattr(item, "max_length"))
        for name in ("summary", "details", "room_number")
    }
    assert limits == {
        "summary": escalation_request._SUMMARY_MAX_CHARS,
        "details": escalation_request._DETAILS_MAX_CHARS,
        "room_number": escalation_request._ROOM_MAX_CHARS,
    }


def test_every_escalation_reason_has_a_card_template() -> None:
    """§2.2: новая причина без строки шаблонов — ошибка теста, а не молчаливый
    пропуск (в проде её поймал бы общий перехват шага как `unexpected`)."""
    assert set(escalation_request._CARD_TEMPLATES) == set(EscalationReason)


async def test_emergency_becomes_urgent_request_in_escalation_category(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """ЧП → срочная заявка ресепшену с текстом гостя дословно и комнатой из
    привязки; строка эскалации ссылается на неё; гость получил текст перехвата."""
    tenant_id, conversation_id = hotel
    replies = await _guest_says(tenant_id, conversation_id, "в номере пожар")

    (request,) = await _requests(tenant_id)
    assert request.summary == "ЧП — сообщение гостя: «в номере пожар»"
    assert request.details is None
    assert request.is_urgent is True
    assert request.origin is requests_api.ServiceRequestOrigin.ESCALATION
    assert request.room_number == ROOM
    with tenant_context(tenant_id):
        (category,) = await requests_api.list_categories()
    assert request.category_id == category.id
    (row,) = await _escalation_rows(tenant_id)
    assert (row.reason, row.request_id) == ("emergency", request.id)
    assert replies == [urgency.emergency_reply("ru", None)]


async def test_llm_unavailable_becomes_ordinary_request(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, conversation_id = hotel
    _llm_down(monkeypatch)
    replies = await _guest_says(tenant_id, conversation_id, "где мой трансфер?")

    (request,) = await _requests(tenant_id)
    assert request.summary == "Бот не смог ответить, гость ждёт сотрудника: «где мой трансфер?»"
    assert request.details is None
    assert request.is_urgent is False
    assert request.origin is requests_api.ServiceRequestOrigin.ESCALATION
    assert request.room_number == ROOM
    (row,) = await _escalation_rows(tenant_id)
    assert (row.reason, row.request_id) == ("llm_unavailable", request.id)
    assert replies == [DEGRADED_REPLY]


@pytest.mark.parametrize(
    "reason", [EscalationReason.UNKNOWN_TOOL, EscalationReason.TOOL_EXECUTION_FAILED]
)
async def test_tool_failure_request_carries_what_the_bot_understood(
    hotel: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    reason: EscalationReason,
) -> None:
    """§2.2: «Что бот понял» — в подробностях; комната привязки сильнее комнаты
    из аргументов инструмента (она проверена, аргумент — нет)."""
    tenant_id, conversation_id = hotel
    _needs_human(monkeypatch, reason, action_summary="постирать рубашку", room_number="999")
    replies = await _guest_says(tenant_id, conversation_id, "да")

    (request,) = await _requests(tenant_id)
    assert request.summary == "Бот не смог оформить просьбу, гость ждёт сотрудника: «да»"
    assert request.details == "Что бот понял: постирать рубашку"
    assert request.is_urgent is False
    assert request.room_number == ROOM
    (row,) = await _escalation_rows(tenant_id)
    assert (row.reason, row.request_id) == (reason.value, request.id)
    assert replies == [NEEDS_HUMAN_REPLY]


async def test_room_falls_back_to_escalation_context_without_binding(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без привязки (Telegram) комната — из контекста эскалации; сырой аргумент
    длиннее поля заявки комнатой не считается — карточка без комнаты, но есть."""
    tenant_id, conversation_id = hotel
    _needs_human(monkeypatch, EscalationReason.TOOL_EXECUTION_FAILED, room_number="1207")
    await _guest_says(tenant_id, conversation_id, "да", room=None)
    _needs_human(monkeypatch, EscalationReason.UNKNOWN_TOOL, room_number="x" * 21)
    # Первую карточку берут, иначе вторая погасла бы дедупликацией (§2.5).
    (first,) = await _requests(tenant_id)
    await _set_status(tenant_id, first.id, requests_api.RequestStatus.IN_PROGRESS)
    await _guest_says(tenant_id, conversation_id, "да", room=None)

    assert [request.room_number for request in await _requests(tenant_id)] == ["1207", None]


async def test_long_guest_text_is_cut_to_fit_the_card(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Длинная реплика не стоит карточки: обрезается с «…» под предел summary."""
    tenant_id, conversation_id = hotel
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "помогите " * 200)

    (request,) = await _requests(tenant_id)
    assert len(request.summary) == 500
    assert request.summary.endswith("…»")


async def test_without_category_key_only_the_escalation_is_written(
    hotel: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§2.3: служба эскалаций не задана — поведение как до spec 0039."""
    tenant_id, conversation_id = hotel
    await _configure(tenant_id, None)
    _llm_down(monkeypatch)
    configure_logging()
    replies = await _guest_says(tenant_id, conversation_id, "алло")

    assert await _requests(tenant_id) == []
    (row,) = await _escalation_rows(tenant_id)
    assert row.request_id is None
    assert replies == [DEGRADED_REPLY]
    skipped = find_record(read_log_records(capsys), "escalation_request_skipped")
    assert skipped["cause"] == "no_category_key"


async def test_missing_category_skips_the_request_but_not_the_guest(
    hotel: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§2.3: ключ есть, службы нет — заявки нет, `ERR-REQUESTS-001` в журнале,
    гость получил прежний ответ, эскалация опубликована."""
    tenant_id, conversation_id = hotel
    await _configure(tenant_id, "security")
    _llm_down(monkeypatch)
    configure_logging()
    replies = await _guest_says(tenant_id, conversation_id, "алло")

    assert await _requests(tenant_id) == []
    (row,) = await _escalation_rows(tenant_id)
    assert row.request_id is None
    assert replies == [DEGRADED_REPLY]
    skipped = find_record(read_log_records(capsys), "escalation_request_skipped")
    assert skipped["cause"] == "category_missing"
    assert skipped["error_code"] == requests_api.ERR_REQUESTS_CATEGORY_NOT_FOUND


async def test_unexpected_failure_does_not_block_the_escalation(
    hotel: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """§2.6: не-`AppError` из создания заявки (IntegrityError) не мешает ни
    эскалации, ни ответу гостю; в журнале — только имя класса, без текста
    исключения: у IntegrityError в нём параметры SQL, то есть текст гостя (§8).

    Маркеры — латиницей: журнал пишет JSON с `ensure_ascii`, кириллица в нём
    экранирована, и проверка «русского текста нет в выводе» была бы зелёной
    всегда. Уровень — WARNING: ERROR ушёл бы в Sentry с телом запроса (#433)."""
    tenant_id, conversation_id = hotel
    guest_marker = "GUEST-TEXT-7731"
    sql_marker = "SQL-PARAM-4410"

    async def broken(_data: requests_api.ServiceRequestCreate) -> None:
        raise IntegrityError(
            f"INSERT INTO service_requests … {sql_marker}",
            {"summary": f"ЧП — сообщение гостя: «{guest_marker} пожар»"},
            Exception(sql_marker),
        )

    monkeypatch.setattr(requests_api, "create_request", broken)
    configure_logging()
    replies = await _guest_says(tenant_id, conversation_id, f"{guest_marker} пожар")

    (row,) = await _escalation_rows(tenant_id)
    assert row.request_id is None
    assert replies == [urgency.emergency_reply("ru", None)]
    output = capsys.readouterr().out
    assert guest_marker not in output
    assert sql_marker not in output
    assert "INSERT" not in output
    records = [line for line in output.splitlines() if "escalation_request_skipped" in line]
    assert len(records) == 1
    assert '"cause": "unexpected"' in records[0]
    assert '"error_type": "IntegrityError"' in records[0]
    assert '"level": "warning"' in records[0]


async def test_repeated_llm_outage_keeps_one_unclaimed_card(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.5: два сообщения подряд при лежащей модели — одна заявка, две строки
    эскалации (счёт честный)."""
    tenant_id, conversation_id = hotel
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "алло")
    await _guest_says(tenant_id, conversation_id, "ау, есть кто?")

    (request,) = await _requests(tenant_id)
    first, second = await _escalation_rows(tenant_id)
    assert (first.request_id, second.request_id) == (request.id, None)


@pytest.mark.parametrize(
    "statuses",
    [
        (requests_api.RequestStatus.IN_PROGRESS,),
        (requests_api.RequestStatus.IN_PROGRESS, requests_api.RequestStatus.DONE),
        (requests_api.RequestStatus.CANCELLED,),
    ],
    ids=["claimed", "done", "cancelled"],
)
async def test_only_an_unclaimed_card_holds_deduplication(
    hotel: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    statuses: tuple[requests_api.RequestStatus, ...],
) -> None:
    """§2.5: взятая или закрытая карточка новую не блокирует — забытая «Готово»
    не должна молча съесть следующие эскалации диалога."""
    tenant_id, conversation_id = hotel
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "алло")
    (first,) = await _requests(tenant_id)
    await _set_status(tenant_id, first.id, *statuses)
    await _guest_says(tenant_id, conversation_id, "ау, есть кто?")

    assert len(await _requests(tenant_id)) == 2


async def test_every_emergency_gets_its_own_urgent_card(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """§2.5: срочные не дедуплицируются — второе ЧП-сообщение часто несёт то,
    чего нет в первом, а в карточке виден только её собственный текст."""
    tenant_id, conversation_id = hotel
    await _guest_says(tenant_id, conversation_id, "пожар")
    await _guest_says(tenant_id, conversation_id, "пожар, в 305 не дышит")

    requests = await _requests(tenant_id)
    assert [request.is_urgent for request in requests] == [True, True]


async def test_emergency_over_unclaimed_ordinary_card_is_still_created(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, conversation_id = hotel
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "алло")
    await _guest_says(tenant_id, conversation_id, "пожар")

    assert [request.is_urgent for request in await _requests(tenant_id)] == [False, True]


async def test_ordinary_escalation_over_unclaimed_urgent_card_adds_nothing(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.5: держит любая невзятая эскалационная карточка, срочная тоже."""
    tenant_id, conversation_id = hotel
    await _guest_says(tenant_id, conversation_id, "пожар")
    _llm_down(monkeypatch)
    await _guest_says(tenant_id, conversation_id, "алло")

    assert [request.is_urgent for request in await _requests(tenant_id)] == [True]
    assert len(await _escalation_rows(tenant_id)) == 2


async def test_escalation_request_is_not_bound_to_the_dialog(
    hotel: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§2.7: `request_origins` не пишется — гостю не придёт «ваша заявка
    выполнена» в ответ на ЧП, и модель не видит карточку среди активных заявок
    диалога (снапшот хода строится ровно по этой привязке, spec 0025)."""
    tenant_id, conversation_id = hotel
    seen: list[Any] = []

    async def remember(**kwargs: Any) -> OrchestratorTurn:
        seen.append(kwargs["active_requests"])
        return OrchestratorTurn(kind=TurnKind.REPLY, reply_text="Чем помочь?")

    await _guest_says(tenant_id, conversation_id, "пожар")
    monkeypatch.setattr(orchestrator, "handle_message", remember)
    await _guest_says(tenant_id, conversation_id, "что случилось с моей заявкой?")

    assert len(await _requests(tenant_id)) == 1
    with tenant_context(tenant_id):
        assert await load_conversation_request_ids(conversation_id) == []
    assert seen == [[]]
