"""Сервис витрины (spec 0069 §4, §7 п. 1–3): сцена и версия, журнал, изоляция тенантов,
подбор из БД, код доступа и подпись cookie."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hospitality.modules.showroom import api as showroom_api
from hospitality.modules.showroom.tests.conftest import create_hotel
from hospitality.platform.models import StaffAction, StaffActionLog, StaffRole
from hospitality.platform.staff_actions import StaffActor
from hospitality.shared.db import session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.demo_showroom import build_demo_scene
from tests.test_staff_auth import create_staff_user, unique_login


async def test_replace_scene_bumps_version_and_writes_journal(hotel: uuid.UUID) -> None:
    user_id = await create_staff_user(unique_login(), tenant_id=hotel, role=StaffRole.MANAGER)
    actor = StaffActor(user_id=user_id, display_name="Менеджер", login="MGR")
    scene = build_demo_scene()
    with tenant_context(hotel):
        assert await showroom_api.get_scene() is None
        assert not await showroom_api.has_scene()
        first = await showroom_api.replace_scene(scene, acting_user=actor)
        second = await showroom_api.replace_scene(scene, acting_user=None)
        current = await showroom_api.get_scene()
        async with session_scope() as session:
            journal = (await session.scalars(select(StaffActionLog))).all()
    assert (first.scene_version, second.scene_version) == (1, 2)
    assert current is not None and current.scene_version == 2
    assert current.scene == scene
    # Строка журнала — только у действия с личностью (spec 0041 §2.2).
    assert [row.action for row in journal] == [StaffAction.SHOWROOM_SCENE_REPLACED]


async def test_scene_is_tenant_isolated(hotel: uuid.UUID) -> None:
    other = await create_hotel("other-hotel")
    with tenant_context(hotel):
        await showroom_api.replace_scene(build_demo_scene(), acting_user=None)
    with tenant_context(other):
        assert await showroom_api.get_scene() is None
        assert await showroom_api.find_rooms(showroom_api.RoomQuery()) == []


async def test_find_rooms_reads_the_stored_scene(hotel: uuid.UUID) -> None:
    with tenant_context(hotel):
        await showroom_api.replace_scene(build_demo_scene(), acting_user=None)
        found = await showroom_api.find_rooms(
            showroom_api.RoomQuery(floor_from=12, side="south", view_key="mountains", count=12)
        )
    assert len(found) == 12
    assert found[0].floor == 15
    assert {match.side for match in found} == {"south"}
    assert all(match.floor >= 12 for match in found)


async def test_access_code_sets_hash_and_signs_cookie_by_version(hotel: uuid.UUID) -> None:
    with tenant_context(hotel):
        assert not await showroom_api.verify_access_code("show-2026-code")
        assert await showroom_api.set_access_code("show-2026-code") == 1
        settings = await showroom_api.load_settings()
        assert settings is not None and settings.access_code_hash is not None
        assert "show-2026-code" not in settings.access_code_hash
        assert await showroom_api.verify_access_code("show-2026-code")
        assert not await showroom_api.verify_access_code("wrong-code-1")
        first_token = showroom_api.access_token(settings, hotel)
        assert showroom_api.token_matches(settings, hotel, first_token)

        assert await showroom_api.set_access_code("another-code-2") == 2
        renewed = await showroom_api.load_settings()
    assert renewed is not None
    # Новый код — прежняя cookie гаснет.
    assert not showroom_api.token_matches(renewed, hotel, first_token)
    assert not showroom_api.token_matches(
        renewed, uuid.uuid4(), showroom_api.access_token(renewed, hotel)
    )


async def test_access_code_shorter_than_eight_is_refused(hotel: uuid.UUID) -> None:
    with tenant_context(hotel):
        try:
            await showroom_api.set_access_code("short")
        except ValueError as error:
            assert "от 8" in str(error)
        else:
            raise AssertionError("короткий код принят")
        settings = await showroom_api.load_settings()
    assert settings is not None and settings.access_code_hash is None
