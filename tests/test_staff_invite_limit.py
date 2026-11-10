"""Лимит выпуска приглашений по (tenant, менеджер) (#163, ERR-AUTH-019): один счётчик
на одиночное приглашение, список и «Новую ссылку»; список проверяется целиком;
бюджет тратит только выпущенная ссылка; у другого менеджера — свой счёт."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Iterator

import pytest

from hospitality.platform.models import StaffRole, Tenant
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_INVALID
from hospitality.platform.staff_invite_batch import InviteRow, create_invites
from hospitality.platform.staff_invite_limit import ERR_AUTH_INVITE_RATE_LIMITED
from hospitality.platform.staff_invite_reissue import reissue_invite
from hospitality.platform.staff_invites import create_invite, list_pending_invites
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from tests.conftest import FakeRateLimitRedis
from tests.test_staff_auth import create_staff_user, unique_login

LIMIT = 3


@pytest.fixture
async def hotel(canonical_database: None) -> tuple[Tenant, uuid.UUID, uuid.UUID]:
    """Тенант и два менеджера: счёт у каждого свой."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="hotel-l", name="Hotel L")
        session.add(tenant)
        await session.flush()
    first = await create_staff_user(unique_login(), tenant_id=tenant.id, role=StaffRole.MANAGER)
    second = await create_staff_user(unique_login(), tenant_id=tenant.id, role=StaffRole.MANAGER)
    return tenant, first, second


@pytest.fixture
def fake_limits(
    canonical_database: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeRateLimitRedis]:
    # Один инстанс на тест: счётчик обязан копиться между вызовами. После
    # `canonical_database`: иначе её teardown видит настройки этого теста.
    fake = FakeRateLimitRedis()
    monkeypatch.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
    monkeypatch.setenv("STAFF_INVITE_ISSUE_RATE_LIMIT_ATTEMPTS", str(LIMIT))
    get_settings.cache_clear()
    yield fake
    get_settings.cache_clear()


def _rows(*logins: str) -> list[InviteRow]:
    return [InviteRow(invited_name=f"Сотрудник {login}", login=login) for login in logins]


async def _expect_limited(call: Awaitable[object]) -> None:
    with pytest.raises(AppError) as error:
        await call
    assert error.value.code == ERR_AUTH_INVITE_RATE_LIMITED
    assert error.value.status_code == 429


async def test_three_doors_share_one_budget_per_manager(
    hotel: tuple[Tenant, uuid.UUID, uuid.UUID],
    fake_limits: FakeRateLimitRedis,
) -> None:
    tenant, manager, other_manager = hotel
    grant = await create_invite(tenant.id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager)
    await create_invites(
        tenant.id, StaffRole.STAFF, _rows("ERLA"), invited_by=manager, service_keys=["reception"]
    )
    await reissue_invite(grant.invite_id, tenant_id=tenant.id, actor_user_id=manager)

    await _expect_limited(
        create_invite(tenant.id, StaffRole.STAFF, "Иван", "IVAN", invited_by=manager)
    )
    await _expect_limited(
        reissue_invite(grant.invite_id, tenant_id=tenant.id, actor_user_id=manager)
    )
    # Отказ лимита ничего не записал: ожидают те же двое.
    assert {item.login for item in await list_pending_invites(tenant.id)} == {"DANA", "ERLA"}

    # Второй менеджер того же отеля — свой счёт.
    other = await create_invite(
        tenant.id, StaffRole.STAFF, "Иван", "IVAN", invited_by=other_manager
    )
    assert other.login == "IVAN"


async def test_list_over_budget_issues_nothing(
    hotel: tuple[Tenant, uuid.UUID, uuid.UUID],
    fake_limits: FakeRateLimitRedis,
) -> None:
    tenant, manager, _ = hotel
    await create_invite(tenant.id, StaffRole.STAFF, "Дана", "DANA", invited_by=manager)

    await _expect_limited(
        create_invites(
            tenant.id,
            StaffRole.STAFF,
            _rows("ERLA", "IVAN", "AIGU"),
            invited_by=manager,
            service_keys=["reception"],
        )
    )
    assert {item.login for item in await list_pending_invites(tenant.id)} == {"DANA"}

    # Влезающий остаток выпускается.
    grants = await create_invites(
        tenant.id,
        StaffRole.STAFF,
        _rows("ERLA", "IVAN"),
        invited_by=manager,
        service_keys=["reception"],
    )
    assert [grant.login for grant in grants] == ["ERLA", "IVAN"]


async def test_rejected_list_spends_no_budget(
    hotel: tuple[Tenant, uuid.UUID, uuid.UUID],
    fake_limits: FakeRateLimitRedis,
) -> None:
    """Список с опечаткой правят и шлют снова — неудачные попытки бюджет не тратят."""
    tenant, manager, _ = hotel
    for _ in range(LIMIT + 1):
        with pytest.raises(AppError) as error:
            await create_invites(
                tenant.id,
                StaffRole.STAFF,
                _rows("ERLA", "1BAD", "IVAN"),
                invited_by=manager,
                service_keys=["reception"],
            )
        assert error.value.code == ERR_AUTH_LOGIN_INVALID

    grants = await create_invites(
        tenant.id,
        StaffRole.STAFF,
        _rows("ERLA", "AIGU", "IVAN"),
        invited_by=manager,
        service_keys=["reception"],
    )
    assert len(grants) == LIMIT


async def test_redis_down_does_not_block_invites(
    hotel: tuple[Tenant, uuid.UUID, uuid.UUID],
    fake_limits: FakeRateLimitRedis,
) -> None:
    """Fail-open, как у всех лимитов (spec 0023 §2): Redis упал — приглашения выпускаются."""
    tenant, manager, _ = hotel
    fake_limits.fail = True
    for login in ("DANA", "ERLA", "IVAN", "AIGU"):
        await create_invite(tenant.id, StaffRole.STAFF, login, login, invited_by=manager)
    assert len(await list_pending_invites(tenant.id)) == LIMIT + 1
