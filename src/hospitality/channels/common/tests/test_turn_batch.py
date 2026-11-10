"""Канал и пакет действий хода (spec 0078 §4, issue #90).

Исход оркестратора подменяется: здесь проверяется, что канал делает с
несколькими заявками хода и с пакетом, ждущим «да» гостя; откуда они берутся у
модели — `ai/tests/test_multi_action.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from hospitality.ai import orchestrator
from hospitality.ai.orchestrator import OrchestratorTurn, PendingAction, TurnKind
from hospitality.channels.common import guest_turn
from hospitality.channels.common.guest_turn import run_guest_turn
from hospitality.channels.common.store import (
    ensure_conversation,
    load_conversation_request_ids,
    load_pending_action,
)
from hospitality.modules.requests import api as requests_api
from hospitality.shared.tenancy import tenant_context
from tests.conftest import FakeRateLimitRedis

BATCH = PendingAction(
    tool_name="create_service_request",
    arguments={"summary": "убрать номер"},
    more=(PendingAction(tool_name="cancel_service_request", arguments={"request_id": "x"}),),
)


async def _request(summary: str) -> uuid.UUID:
    categories = await requests_api.list_categories() or [
        await requests_api.create_category(
            requests_api.RequestCategoryCreate(key="housekeeping", name="Housekeeping")
        )
    ]
    created = await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=categories[0].id,
            origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
            summary=summary,
        )
    )
    return created.id


async def _turn(conversation_id: uuid.UUID, text: str) -> uuid.UUID | None:
    async def reply(_: str) -> None:
        return None

    return await run_guest_turn(
        conversation_id,
        text,
        uuid.uuid4(),
        external_id="stay-1",
        rate_limit_key="stay-1",
        reply=reply,
        provider=None,
        verified_room_number="305",
        verified_stay_id=None,
    )


@pytest.fixture
def tenant(demo_tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Тенант и Redis лимита чата в памяти. Не autouse: `monkeypatch` обязан
    подняться ПОСЛЕ временной БД, иначе откат её имени идёт после DROP."""
    fake_redis = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake_redis)
    return demo_tenant


async def test_every_request_of_the_turn_is_linked_to_the_dialog(
    tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "stay-1")
        first, second = await _request("убрать номер"), await _request("полотенца")

        async def done(**_: Any) -> OrchestratorTurn:
            return OrchestratorTurn(
                kind=TurnKind.ACTION_DONE, reply_text="Готово.", created_request_ids=(first, second)
            )

        monkeypatch.setattr(orchestrator, "handle_message", done)
        returned = await _turn(conversation_id, "да")
        linked = await load_conversation_request_ids(conversation_id)
    assert sorted(linked) == sorted([first, second])
    assert returned == first  # подсказке веб-гостю хватает первой (#223)


async def test_batch_waits_between_turns_whole(
    tenant: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[PendingAction | None] = []

    async def turn(**kwargs: Any) -> OrchestratorTurn:
        seen.append(kwargs["pending_action"])
        return OrchestratorTurn(
            kind=TurnKind.AWAITING_CONFIRMATION, reply_text="Оформить?", pending_action=BATCH
        )

    monkeypatch.setattr(orchestrator, "handle_message", turn)
    with tenant_context(tenant):
        conversation_id = await ensure_conversation("web", "stay-1")
        await _turn(conversation_id, "убери номер и отмени прошлую")
        stored = await load_pending_action(conversation_id)
        await _turn(conversation_id, "да")
    assert stored is not None and len(stored["more"]) == 1
    assert seen == [None, BATCH]


def test_single_action_is_stored_as_before_and_old_rows_still_read() -> None:
    single = PendingAction(tool_name="create_service_request", arguments={"summary": "вода"})
    row = {"tool_name": "create_service_request", "arguments": {"summary": "вода"}}
    # Одиночное действие пишется как до spec 0078 — без ключа `more`.
    assert guest_turn._serialize_pending(single) == row
    assert guest_turn._deserialize_pending(row) == single
