"""Тесты AI-инструментов (Task 0015, R-7): резолв category_key→id и контракт;
отмена по снапшоту диалога (issue #40, spec 0025)."""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai.tools import cancel_service_request, create_service_request, registry
from hospitality.ai.tools.base import ActiveRequest, ConfirmationClass, ToolTurnContext
from hospitality.ai.tools.create_service_request import ERR_AI_INVALID_TOOL_CALL
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

# Пустой контекст хода: у диалога нет открытых заявок (создание его не читает).
_EMPTY_CONTEXT = ToolTurnContext()


async def _create_request(summary: str = "убрать номер") -> requests_api.ServiceRequestRead:
    categories = await requests_api.list_categories()
    category_id = next(c.id for c in categories if c.key == "housekeeping")
    return await requests_api.create_request(
        requests_api.ServiceRequestCreate(category_id=category_id, summary=summary)
    )


def _as_active(request: requests_api.ServiceRequestRead) -> ActiveRequest:
    """Снапшот-представление заявки, как его собирает канал (spec 0025)."""
    return ActiveRequest(
        id=request.id,
        status=request.status,
        summary=request.summary,
        daily_number=request.daily_number,
        room_number=request.room_number,
    )


async def test_execute_resolves_category_key_and_creates_request(demo_tenant: uuid.UUID) -> None:
    with tenant_context(demo_tenant):
        result = await create_service_request.execute(
            {"category_key": "housekeeping", "summary": "убрать номер", "room_number": "301"},
            _EMPTY_CONTEXT,
        )
        assert result.room_number == "301"
        page = await requests_api.list_requests(limit=10, offset=0)
    assert page.total == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("kk", "kk"),  # канонический код — как есть
        ("KK", "kk"),  # регистр нормализуется
        ("kk-KZ", "kk"),  # региональный суффикс отбрасывается
        ("kazakh", None),  # не-код: заявка важнее метки — язык просто не пишется
        (42, None),  # мусорный тип не валит создание заявки
    ],
)
async def test_execute_normalizes_guest_language(
    demo_tenant: uuid.UUID, raw: object, expected: str | None
) -> None:
    """Язык гостя терпимо нормализуется (spec 0021 П-1): кривое значение модели
    не роняет создание заявки — просто остаётся NULL (уведомление уйдёт на
    default_language тенанта)."""
    with tenant_context(demo_tenant):
        result = await create_service_request.execute(
            {
                "category_key": "housekeeping",
                "summary": "убрать номер",
                "guest_language": raw,
            },
            _EMPTY_CONTEXT,
        )
    assert result.guest_language == expected


async def test_tool_spec_requires_guest_language(demo_tenant: uuid.UUID) -> None:
    """Схема инструмента требует guest_language: модель обязана назвать язык гостя
    (терпимость к мусору — в execute, а обязательность — в контракте)."""
    spec = create_service_request.build_spec(["housekeeping"])
    assert "guest_language" in spec.input_schema["required"]
    assert "guest_language" in spec.input_schema["properties"]


async def test_execute_unknown_key_raises_invalid_tool_call(demo_tenant: uuid.UUID) -> None:
    with tenant_context(demo_tenant), pytest.raises(AppError) as error:
        await create_service_request.execute(
            {"category_key": "spa", "summary": "массаж"}, _EMPTY_CONTEXT
        )
    assert error.value.code == ERR_AI_INVALID_TOOL_CALL
    assert error.value.status_code == 422


async def test_execute_invalid_arguments_raises_invalid_tool_call(demo_tenant: uuid.UUID) -> None:
    # summary обязателен — модель нарушила контракт инструмента.
    with tenant_context(demo_tenant), pytest.raises(AppError) as error:
        await create_service_request.execute({"category_key": "housekeeping"}, _EMPTY_CONTEXT)
    assert error.value.code == ERR_AI_INVALID_TOOL_CALL


async def test_build_tool_specs_without_active_requests(demo_tenant: uuid.UUID) -> None:
    """Без открытых заявок диалога инструмента отмены нет: пустой enum допустимых
    id бессмыслен и провоцирует галлюцинации (spec 0025)."""
    with tenant_context(demo_tenant):
        specs = await registry.build_tool_specs(_EMPTY_CONTEXT)
    assert [spec.name for spec in specs] == ["create_service_request"]
    enum = specs[0].input_schema["properties"]["category_key"]["enum"]
    assert set(enum) == {"housekeeping", "engineering"}


async def test_build_tool_specs_with_active_requests_adds_cancel(demo_tenant: uuid.UUID) -> None:
    """Открытые заявки диалога включают инструмент отмены; enum `request_id` —
    ровно их id (анти-галлюцинация §7.4, как enum category_key)."""
    with tenant_context(demo_tenant):
        request = await _create_request()
        context = ToolTurnContext(active_requests=(_as_active(request),))
        specs = await registry.build_tool_specs(context)
    assert [spec.name for spec in specs] == ["create_service_request", "cancel_service_request"]
    cancel_schema = specs[1].input_schema
    assert cancel_schema["properties"]["request_id"]["enum"] == [str(request.id)]
    assert "confirmation_question" in cancel_schema["required"]


async def test_cancel_executes_only_for_request_from_snapshot(demo_tenant: uuid.UUID) -> None:
    """Happy path отмены: id из снапшота хода → заявка `cancelled`, причина —
    фиксированное примечание для персонала (spec 0025)."""
    with tenant_context(demo_tenant):
        request = await _create_request()
        context = ToolTurnContext(active_requests=(_as_active(request),))
        result = await cancel_service_request.execute({"request_id": str(request.id)}, context)
        stored = await requests_api.get_request(request.id)
    assert result.status is requests_api.RequestStatus.CANCELLED
    assert stored.status is requests_api.RequestStatus.CANCELLED
    assert stored.resolution_note == cancel_service_request.CANCELLED_BY_GUEST_NOTE


async def test_cancel_foreign_request_is_rejected_and_stays_open(demo_tenant: uuid.UUID) -> None:
    """DoD issue #40: заявку НЕ из снапшота диалога отменить нельзя — даже
    существующую у того же тенанта (чужой диалог). ERR-AI-004, заявка открыта."""
    with tenant_context(demo_tenant):
        foreign = await _create_request("чужая заявка")  # привязана к другому диалогу
        context = ToolTurnContext()  # снапшот ЭТОГО диалога пуст
        with pytest.raises(AppError) as error:
            await cancel_service_request.execute({"request_id": str(foreign.id)}, context)
        stored = await requests_api.get_request(foreign.id)
    assert error.value.code == ERR_AI_INVALID_TOOL_CALL
    assert stored.status is requests_api.RequestStatus.NEW  # ничего не отменено


async def test_cancel_invalid_arguments_raise_invalid_tool_call(demo_tenant: uuid.UUID) -> None:
    with tenant_context(demo_tenant), pytest.raises(AppError) as error:
        await cancel_service_request.execute({"request_id": "not-a-uuid"}, _EMPTY_CONTEXT)
    assert error.value.code == ERR_AI_INVALID_TOOL_CALL


async def test_registry_tool_contracts(demo_tenant: uuid.UUID) -> None:
    assert registry.confirmation_class("create_service_request") is ConfirmationClass.CONFIRM_GUEST
    assert registry.confirmation_class("cancel_service_request") is ConfirmationClass.CONFIRM_GUEST
    # По этому флагу оркестратор заполняет created_request_id, а канал — привязку
    # request_origins: отмена НЕ должна выглядеть как созданная заявка (spec 0025).
    assert registry.creates_request("create_service_request") is True
    assert registry.creates_request("cancel_service_request") is False
    assert registry.done_text("create_service_request") != registry.done_text(
        "cancel_service_request"
    )
    with pytest.raises(AppError) as error:
        registry.confirmation_class("nope")
    assert error.value.code == ERR_AI_INVALID_TOOL_CALL
    with tenant_context(demo_tenant), pytest.raises(AppError):
        await registry.execute("nope", {}, _EMPTY_CONTEXT)
