"""Жизненный цикл заявки (Task 0012, §5.2): создание, валидные и невалидные
переходы статусов, ошибки с кодами каталога.

Весь доступ — через публичный интерфейс `api.py`, как у настоящего
потребителя модуля (HTTP API, AI-инструмент).
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    ERR_REQUESTS_CATEGORY_NOT_FOUND,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    ERR_REQUESTS_REQUEST_NOT_FOUND,
    RequestCategoryCreate,
    RequestStatus,
    ServiceRequestCreate,
    change_request_status,
    create_category,
    create_request,
    get_request,
)
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


async def test_request_is_created_in_status_new(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """DoD задачи: заявку можно создать вызовом сервиса в тесте."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                summary="Please clean room 204",
                details="Guest asks for full cleaning after lunch",
                room_number="204",
            )
        )
        stored = await get_request(request.id)

    assert request.status is RequestStatus.NEW
    assert request.category_id == category.id
    assert stored == request
    assert stored.created_at.tzinfo is not None  # канон времени §9: aware UTC


async def test_create_request_with_unknown_category_fails(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await create_request(
            ServiceRequestCreate(category_id=uuid.uuid4(), summary="no such category")
        )
    assert error.value.code == ERR_REQUESTS_CATEGORY_NOT_FOUND
    assert error.value.status_code == 404


async def test_full_lifecycle_new_assigned_in_progress_done(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(category_id=category.id, summary="Fix the shower")
        )
        for expected_status in (
            RequestStatus.ASSIGNED,
            RequestStatus.IN_PROGRESS,
            RequestStatus.DONE,
        ):
            updated = await change_request_status(request.id, expected_status)
            assert updated.status is expected_status
        assert (await get_request(request.id)).status is RequestStatus.DONE


@pytest.mark.parametrize(
    "start_status_path",
    [
        (),  # new
        (RequestStatus.ASSIGNED,),
        (RequestStatus.ASSIGNED, RequestStatus.IN_PROGRESS),
    ],
)
async def test_any_non_terminal_status_can_be_cancelled(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    start_status_path: tuple[RequestStatus, ...],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(category_id=category.id, summary="cancel me")
        )
        for status in start_status_path:
            await change_request_status(request.id, status)
        cancelled = await change_request_status(request.id, RequestStatus.CANCELLED)
    assert cancelled.status is RequestStatus.CANCELLED


@pytest.mark.parametrize(
    ("status_path", "invalid_target"),
    [
        ((), RequestStatus.DONE),  # new → done, минуя работу
        ((), RequestStatus.IN_PROGRESS),  # new → in_progress, минуя назначение
        ((), RequestStatus.NEW),  # переход «в тот же статус»
        ((RequestStatus.ASSIGNED,), RequestStatus.DONE),  # assigned → done
        # Терминальные статусы: из done и cancelled пути нет.
        (
            (RequestStatus.ASSIGNED, RequestStatus.IN_PROGRESS, RequestStatus.DONE),
            RequestStatus.IN_PROGRESS,
        ),
        ((RequestStatus.CANCELLED,), RequestStatus.ASSIGNED),
    ],
)
async def test_invalid_transitions_are_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    status_path: tuple[RequestStatus, ...],
    invalid_target: RequestStatus,
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(category_id=category.id, summary="strict lifecycle")
        )
        for status in status_path:
            await change_request_status(request.id, status)
        last_valid_status = (await get_request(request.id)).status

        with pytest.raises(AppError) as error:
            await change_request_status(request.id, invalid_target)
        assert error.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION
        assert error.value.status_code == 409
        # Отвергнутый переход ничего не меняет.
        assert (await get_request(request.id)).status is last_valid_status


async def test_change_status_of_missing_request_fails(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await change_request_status(uuid.uuid4(), RequestStatus.ASSIGNED)
    assert error.value.code == ERR_REQUESTS_REQUEST_NOT_FOUND
    assert error.value.status_code == 404


async def test_duplicate_category_key_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    await make_category(tenant_a, key="it-support", name="IT")

    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await create_category(RequestCategoryCreate(key="it-support", name="IT again"))
    assert error.value.code == ERR_REQUESTS_CATEGORY_KEY_TAKEN
    assert error.value.status_code == 409

    # Ключ уникален в пределах тенанта: у соседа тот же key — не конфликт.
    other = await make_category(tenant_b, key="it-support", name="IT")
    assert other.key == "it-support"
