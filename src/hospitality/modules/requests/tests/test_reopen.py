"""Возврат выполненной заявки в работу (spec 0080 §2, issue #55).

`done → in_progress` сутки после закрытия: метки и примечание закрытия снимаются,
взятие остаётся, растёт `reopen_count`, событие и строка журнала — одной
транзакцией. Запрос с витрины, прошедшие сутки, не выполненная, чужая служба — отказ
без записи; заявка формы гостя (заказ, бронь, АВР) возвращается (#998). Общая карта
переходов закрытую по-прежнему не воскрешает.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from hospitality.modules.requests import reopen
from hospitality.modules.requests.api import (
    ERR_REQUESTS_FOREIGN_SERVICE,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    ERR_REQUESTS_REOPEN_NOT_ALLOWED,
    REOPEN_WINDOW,
    CancelReason,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    StaffStatsRow,
    can_reopen,
    change_request_status,
    create_request,
    get_request,
    reopen_request,
    staff_stats,
)
from hospitality.modules.requests.tests.conftest import make_acting_user, make_category
from hospitality.platform.models import StaffAction, StaffActionLog
from hospitality.platform.staff_actions import StaffActor
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import OutboxEvent
from hospitality.shared.tenancy import tenant_context

_NOTE = "полотенца только два"


async def _done(
    tenant_id: uuid.UUID,
    *,
    origin: ServiceRequestOrigin = ServiceRequestOrigin.GUEST_CHAT,
    actor: StaffActor | None = None,
    category_key: str = "housekeeping",
) -> ServiceRequestRead:
    """Заявка, взятая и закрытая «Готово» с примечанием — от имени `actor`."""
    category = await make_category(tenant_id, key=category_key)
    with tenant_context(tenant_id):
        created = await create_request(
            ServiceRequestCreate(
                category_id=category.id, origin=origin, summary="полотенца", room_number="305"
            )
        )
        await change_request_status(created.id, RequestStatus.IN_PROGRESS, acting_user=actor)
        return await change_request_status(
            created.id, RequestStatus.DONE, resolution_note=_NOTE, acting_user=actor
        )


async def _staff_actor(name: str, login: str) -> StaffActor:
    acting = await make_acting_user(name)
    return StaffActor(user_id=acting.user_id, display_name=name, login=login)


async def test_reopen_returns_the_request_to_the_one_who_did_it(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    maid = await _staff_actor("Айгуль", "AIG001")
    closed = await _done(tenant_a, actor=maid)
    senior = await _staff_actor("Старшая", "SEN001")

    with tenant_context(tenant_a):
        reopened = await reopen_request(closed.id, acting_user=senior)
        stored = await get_request(closed.id)

    assert stored == reopened
    assert reopened.status is RequestStatus.IN_PROGRESS
    # Заявка снова открыта: «когда и кем закрыта» и примечание закрытия сняты…
    assert (reopened.closed_at, reopened.closed_by_display_name) == (None, None)
    assert reopened.resolution_note is None
    # …а взятие осталось — заявка у того, кто её делал.
    assert reopened.claimed_by_display_name == "Айгуль"
    assert reopened.claimed_at == closed.claimed_at
    assert reopened.reopen_count == 1


async def test_reopen_publishes_the_transition_and_journals_it(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    closed = await _done(tenant_a)
    senior = await make_acting_user("Старшая")

    with tenant_context(tenant_a):
        await reopen_request(closed.id, acting_user=senior)
        async with session_scope() as session:
            actions = list(await session.scalars(select(StaffActionLog.action)))
    async with platform_session_scope() as session:
        last = (
            await session.scalars(select(OutboxEvent).order_by(OutboxEvent.occurred_at.desc()))
        ).first()

    assert last is not None and last.event_name == "request.status_changed"
    assert (last.payload["old_status"], last.payload["new_status"]) == ("done", "in_progress")
    assert actions == [StaffAction.REQUEST_REOPENED]


async def test_closed_again_after_reopen_gets_a_new_close_and_a_second_reopen_counts(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    closed = await _done(tenant_a)

    with tenant_context(tenant_a):
        await reopen_request(closed.id)
        again = await change_request_status(closed.id, RequestStatus.DONE)
        second = await reopen_request(closed.id)

    assert again.closed_at is not None and closed.closed_at is not None
    assert again.closed_at > closed.closed_at
    # Новое «Готово» без примечания — уже не «выполнена частично».
    assert again.resolution_note is None
    assert second.reopen_count == 2


async def test_reopen_is_refused_after_the_window(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a, _ = two_tenants
    closed = await _done(tenant_a)
    assert closed.closed_at is not None
    late = closed.closed_at + REOPEN_WINDOW
    monkeypatch.setattr(reopen, "utc_now", lambda: late)

    with tenant_context(tenant_a):
        with pytest.raises(AppError) as refused:
            await reopen_request(closed.id)
        stored = await get_request(closed.id)

    assert refused.value.code == ERR_REQUESTS_REOPEN_NOT_ALLOWED
    assert refused.value.status_code == 409
    assert stored.status is RequestStatus.DONE
    assert stored.reopen_count == 0


async def test_showroom_request_is_not_reopened(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Запрос с витрины (`showroom_form`) не возвращается — вне #998."""
    tenant_a, _ = two_tenants
    closed = await _done(tenant_a, origin=ServiceRequestOrigin.SHOWROOM_FORM)

    with tenant_context(tenant_a), pytest.raises(AppError) as refused:
        await reopen_request(closed.id)

    assert refused.value.code == ERR_REQUESTS_REOPEN_NOT_ALLOWED


async def test_guest_form_request_is_reopened(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Заказ, бронь, АВР (`guest_form`) — поправку гостю шлют их подписчики (#998)."""
    tenant_a, _ = two_tenants
    closed = await _done(tenant_a, origin=ServiceRequestOrigin.GUEST_FORM)

    with tenant_context(tenant_a):
        reopened = await reopen_request(closed.id)

    assert reopened.status is RequestStatus.IN_PROGRESS
    assert reopened.reopen_count == 1


async def test_only_done_is_reopened_and_foreign_service_first(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    closed = await _done(tenant_a)
    other = await make_category(tenant_a, key="engineering", name="Инженерия")

    with tenant_context(tenant_a):
        with pytest.raises(AppError) as foreign:
            await reopen_request(closed.id, allowed_category_ids={other.id})
        await reopen_request(closed.id)
        with pytest.raises(AppError) as twice:
            await reopen_request(closed.id)
        await change_request_status(
            closed.id, RequestStatus.CANCELLED, cancel_reason=CancelReason.DUPLICATE
        )
        with pytest.raises(AppError) as cancelled:
            await reopen_request(closed.id)

    assert foreign.value.code == ERR_REQUESTS_FOREIGN_SERVICE
    assert twice.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION
    assert cancelled.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION


async def test_stale_claim_does_not_resurrect_a_done_request(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Возврат — только своим входом: «Взять» по общей карте закрытую не открывает."""
    tenant_a, _ = two_tenants
    closed = await _done(tenant_a)

    with tenant_context(tenant_a), pytest.raises(AppError) as refused:
        await change_request_status(closed.id, RequestStatus.IN_PROGRESS)

    assert refused.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION


async def test_can_reopen_is_the_same_rule(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    closed = await _done(tenant_a)
    form = await _done(tenant_a, origin=ServiceRequestOrigin.GUEST_FORM, category_key="orders")
    showroom = await _done(
        tenant_a, origin=ServiceRequestOrigin.SHOWROOM_FORM, category_key="sales"
    )
    assert closed.closed_at is not None
    now = utc_now()

    assert can_reopen(closed, now) is True
    assert can_reopen(closed, closed.closed_at + REOPEN_WINDOW - timedelta(seconds=1)) is True
    assert can_reopen(closed, closed.closed_at + REOPEN_WINDOW) is False
    assert can_reopen(form, now) is True
    assert can_reopen(showroom, now) is False
    assert can_reopen(closed.model_copy(update={"status": RequestStatus.IN_PROGRESS})) is False
    assert can_reopen(closed.model_copy(update={"closed_at": None})) is False


async def test_reopened_done_is_not_counted_for_the_one_who_closed_it(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Итоги по сотруднику: «Готово», которое вернули, — не «Выполнено» (spec 0080 §2.4)."""
    tenant_a, _ = two_tenants
    maid = await _staff_actor("Айгуль", "AIG001")
    closed = await _done(tenant_a, actor=maid)
    since, until = utc_now() - timedelta(hours=1), utc_now() + timedelta(hours=1)

    with tenant_context(tenant_a):
        await reopen_request(closed.id, acting_user=maid)
        reopened = await staff_stats(since=since, until=until, category_ids=None)
        await change_request_status(closed.id, RequestStatus.DONE, acting_user=maid)
        closed_again = await staff_stats(since=since, until=until, category_ids=None)

    def done_of(rows: list[StaffStatsRow]) -> int:
        return next(row.done for row in rows if row.login == "AIG001")

    assert done_of(reopened) == 0
    assert done_of(closed_again) == 1
