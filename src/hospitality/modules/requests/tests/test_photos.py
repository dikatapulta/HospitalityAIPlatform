"""Фото гостя к заявке — домен (spec 0081 §5, §10; issue #82)."""

# ruff: noqa: F811

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from hospitality.modules.requests.api import (
    ERR_REQUESTS_PHOTO_NOT_FOUND,
    MAX_REQUEST_PHOTOS,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    attach_request_photos,
    change_request_status,
    create_request,
    list_attached_file_ids,
    list_request_photo_ids,
    read_request_photo,
)
from hospitality.modules.requests.models import RequestPhoto
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.platform.files import FilePurpose, delete_file, store_upload
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.conftest import fake_file_storage  # noqa: F401  (фикстура для pytest)
from tests.test_file_content import make_png

PNG = make_png()

pytestmark = pytest.mark.usefixtures("fake_file_storage")


async def _request(category_id: uuid.UUID) -> uuid.UUID:
    created = await create_request(
        ServiceRequestCreate(
            origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category_id, summary="кран течёт"
        )
    )
    return created.id


async def _photo(purpose: FilePurpose = FilePurpose.REQUEST_PHOTO) -> uuid.UUID:
    return (await store_upload(purpose, PNG, "tap.png")).id


async def _link_count() -> int:
    async with session_scope() as session:
        return int(await session.scalar(select(func.count()).select_from(RequestPhoto)) or 0)


async def test_attaches_up_to_five_in_order_sixth_skipped(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        photos = [await _photo() for _ in range(MAX_REQUEST_PHOTOS + 1)]

        first = await attach_request_photos(request_id, photos[:3])
        rest = await attach_request_photos(request_id, photos[3:])
        listed = await list_request_photo_ids([request_id])

    assert (first.attached_file_ids, first.photo_count) == (photos[:3], 3)
    assert (rest.attached_file_ids, rest.photo_count) == (photos[3:5], MAX_REQUEST_PHOTOS)
    assert listed == {request_id: photos[:MAX_REQUEST_PHOTOS]}


async def test_repeat_is_idempotent_and_in_progress_accepts(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        await change_request_status(request_id, RequestStatus.IN_PROGRESS)
        photo = await _photo()
        first = await attach_request_photos(request_id, [photo, photo])
        again = await attach_request_photos(request_id, [photo])
        links = await _link_count()

    assert (first.attached_file_ids, first.photo_count) == ([photo], 1)
    assert (again.attached_file_ids, again.photo_count) == ([], 1)
    assert links == 1


async def test_two_attaches_at_once_do_not_pass_the_limit(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Блокировка строки заявки: два приложения разом вместе не дают больше пяти."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        photos = [await _photo() for _ in range(8)]
        results = await asyncio.gather(
            attach_request_photos(request_id, photos[:4]),
            attach_request_photos(request_id, photos[4:]),
        )
        links = await _link_count()

    assert links == MAX_REQUEST_PHOTOS
    assert sum(len(result.attached_file_ids) for result in results) == MAX_REQUEST_PHOTOS


async def test_closed_or_unknown_request_gives_empty_result(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        done = await _request(category.id)
        await change_request_status(done, RequestStatus.IN_PROGRESS)
        await change_request_status(done, RequestStatus.DONE)
        cancelled = await _request(category.id)
        await change_request_status(cancelled, RequestStatus.CANCELLED)
        photo = await _photo()

        results = [
            await attach_request_photos(request_id, [photo])
            for request_id in (done, cancelled, uuid.uuid4())
        ]
        links = await _link_count()

    assert all((r.attached_file_ids, r.photo_count) == ([], 0) for r in results)
    assert links == 0


async def test_only_own_request_photo_files_are_attached(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Внешний ключ проверяется в обход RLS: файл другого отеля или другого
    назначения связью не становится."""
    tenant_a, tenant_b = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_b):
        foreign = await _photo()
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        logo = await _photo(FilePurpose.HOTEL_LOGO)
        own = await _photo()
        result = await attach_request_photos(request_id, [foreign, logo, own])

    assert (result.attached_file_ids, result.photo_count) == ([own], 1)


async def test_attached_file_ids_lists_only_linked(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        first = await _request(category.id)
        second = await _request(category.id)
        shared, waiting = await _photo(), await _photo()
        await attach_request_photos(first, [shared])
        await attach_request_photos(second, [shared])

        attached = await list_attached_file_ids([shared, waiting])
        listed = await list_request_photo_ids([first, second, uuid.uuid4()])

    assert attached == {shared}
    assert listed == {first: [shared], second: [shared]}


async def test_deleting_file_removes_link(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Ретеншн узла удаляет файл — связь уходит каскадом (spec 0081 §9)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        photo = await _photo()
        await attach_request_photos(request_id, [photo])
        await delete_file(photo)

        assert await list_request_photo_ids([request_id]) == {}
        assert await _link_count() == 0


async def test_other_tenant_does_not_see_link(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    category = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request_id = await _request(category.id)
        photo = await _photo()
        await attach_request_photos(request_id, [photo])

    with tenant_context(tenant_b):
        assert await _link_count() == 0
        assert await list_request_photo_ids([request_id]) == {}
        assert await list_attached_file_ids([photo]) == set()
        with pytest.raises(AppError) as caught:
            await read_request_photo(request_id, photo)
    assert caught.value.code == ERR_REQUESTS_PHOTO_NOT_FOUND


async def test_read_returns_linked_photo_only_in_allowed_services(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    engineering = await make_category(tenant_a, key="engineering", name="Engineering")
    housekeeping = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request_id = await _request(engineering.id)
        photo, stray = await _photo(), await _photo()
        await attach_request_photos(request_id, [photo])

        stored = await read_request_photo(
            request_id, photo, allowed_category_ids=frozenset({engineering.id})
        )
        refusals = []
        for file_id, allowed in (
            (stray, None),
            (photo, frozenset({housekeeping.id})),
            (photo, frozenset()),
        ):
            with pytest.raises(AppError) as caught:
                await read_request_photo(request_id, file_id, allowed_category_ids=allowed)
            refusals.append((caught.value.code, caught.value.status_code))

    assert stored.meta.id == photo
    assert stored.meta.content_type == "image/webp"
    assert refusals == [(ERR_REQUESTS_PHOTO_NOT_FOUND, 404)] * 3
