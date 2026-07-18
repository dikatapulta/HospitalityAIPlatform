"""Тесты AI-инструментов (Task 0015, R-7): резолв category_key→id и контракт."""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai.tools import create_service_request, registry
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.ai.tools.create_service_request import ERR_AI_INVALID_TOOL_CALL
from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


async def test_execute_resolves_category_key_and_creates_request(demo_tenant: uuid.UUID) -> None:
    with tenant_context(demo_tenant):
        result = await create_service_request.execute(
            {"category_key": "housekeeping", "summary": "убрать номер", "room_number": "301"}
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
            }
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
        await create_service_request.execute({"category_key": "spa", "summary": "массаж"})
    assert error.value.code == ERR_AI_INVALID_TOOL_CALL
    assert error.value.status_code == 422


async def test_execute_invalid_arguments_raises_invalid_tool_call(demo_tenant: uuid.UUID) -> None:
    # summary обязателен — модель нарушила контракт инструмента.
    with tenant_context(demo_tenant), pytest.raises(AppError) as error:
        await create_service_request.execute({"category_key": "housekeeping"})
    assert error.value.code == ERR_AI_INVALID_TOOL_CALL


async def test_build_tool_specs_exposes_tenant_category_keys(demo_tenant: uuid.UUID) -> None:
    with tenant_context(demo_tenant):
        specs = await registry.build_tool_specs()
    assert len(specs) == 1
    enum = specs[0].input_schema["properties"]["category_key"]["enum"]
    assert set(enum) == {"housekeeping", "engineering"}


async def test_registry_confirmation_class_and_unknown_tool(demo_tenant: uuid.UUID) -> None:
    assert registry.confirmation_class("create_service_request") is ConfirmationClass.CONFIRM_GUEST
    with pytest.raises(AppError) as error:
        registry.confirmation_class("nope")
    assert error.value.code == ERR_AI_INVALID_TOOL_CALL
    with tenant_context(demo_tenant), pytest.raises(AppError):
        await registry.execute("nope", {})
