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
