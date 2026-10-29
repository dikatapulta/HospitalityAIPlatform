"""Оплата при выполнении (spec 0038 Р-2, §4, §6 «PR 2»; issue #418).

Заявку с признаком `payment_required` домен не закрывает `done` без отметки
«Оплачено» (ERR-REQUESTS-006) ни из одного канала; отметка — `mark_request_paid`
с личностью сотрудника и событием `request.payment_marked` в той же транзакции;
неприменимая отметка и отмена оплаченной гостем — ERR-REQUESTS-008. Сюда же —
идемпотентный `request_id` у `create_request` (P-8) и `only_from` у перехода.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from hospitality.modules.requests import service as requests_service
from hospitality.modules.requests.api import (
    ERR_REQUESTS_DONE_WITHOUT_PAYMENT,
    ERR_REQUESTS_FOREIGN_SERVICE,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    ERR_REQUESTS_PAYMENT_NOT_APPLICABLE,
    ERR_REQUESTS_REQUEST_NOT_FOUND,
    ActingUser,
    RequestInitiator,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    ServiceRequestRead,
    change_request_status,
    create_request,
    get_request,
    mark_request_paid,
)
from hospitality.modules.requests.models import ServiceRequest
from hospitality.modules.requests.tests.conftest import make_acting_user, make_category
from hospitality.modules.requests.tests.test_handover import _journal, _outbox
from hospitality.platform.models import StaffAction
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context
from tests.test_tenant_config import _wait_until_a_backend_blocks


async def _order(
    category_id: uuid.UUID, *, request_id: uuid.UUID | None = None
) -> ServiceRequestRead:
    """Заявка заказа в номер: с формы гостя и с оплатой при выполнении."""
    return await create_request(
        ServiceRequestCreate(
            category_id=category_id,
            origin=ServiceRequestOrigin.GUEST_FORM,
            summary="Борщ ×1, Чай ×2",
            room_number="512",
            payment_required=True,
        ),
        request_id=request_id,
    )


async def _claimed_order(category_id: uuid.UUID, waiter: ActingUser) -> ServiceRequestRead:
    order = await _order(category_id)
    return await change_request_status(order.id, RequestStatus.IN_PROGRESS, acting_user=waiter)


def _event_names(rows: list[tuple[str, dict[str, object]]]) -> list[str]:
    return [name for name, _ in rows]


async def test_done_without_payment_mark_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Р-2: «Готово» без «Оплачено» — 006, заявка открыта, события закрытия нет.
    Личности нет намеренно: защита в домене, а не у кнопки кабинета."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    with tenant_context(tenant_a):
        order = await _order(fnb.id)
        await change_request_status(order.id, RequestStatus.IN_PROGRESS)
        with pytest.raises(AppError) as error:
            await change_request_status(order.id, RequestStatus.DONE)
        after = await get_request(order.id)

    assert error.value.code == ERR_REQUESTS_DONE_WITHOUT_PAYMENT
    assert error.value.status_code == 409
    assert after.status is RequestStatus.IN_PROGRESS
    assert after.closed_at is None
    assert _event_names(await _outbox(order.id)) == ["request.created", "request.status_changed"]


async def test_payment_mark_then_done_records_who_and_when(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """«Иду за оплатой» → «Оплачено» → «Доставлено»: имена шагов в заявке, событие отметки."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    waiter = await make_acting_user("Айгерим")
    runner = await make_acting_user("Ержан")
    with tenant_context(tenant_a):
        order = await _claimed_order(fnb.id, waiter)
        paid = await mark_request_paid(order.id, acting_user=waiter)
        done = await change_request_status(order.id, RequestStatus.DONE, acting_user=runner)

    assert paid.payment_required is True
    assert paid.paid_at is not None
    assert paid.paid_by_display_name == "Айгерим"
    assert paid.status is RequestStatus.IN_PROGRESS
    assert done.status is RequestStatus.DONE
    assert done.paid_at == paid.paid_at
    assert done.claimed_by_display_name == "Айгерим"
    assert done.closed_by_display_name == "Ержан"
    events = await _outbox(order.id)
    assert ("request.payment_marked", {"request_id": str(order.id)}) in events
    assert _event_names(events).count("request.payment_marked") == 1


async def test_payment_mark_writes_journal_row_in_same_transaction(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0041 §2.2: «Оплачено» — строка журнала со снапшотом заявки; отказ
    отметки (008) строки не оставляет."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    waiter = await make_acting_user("Айгерим")
    with tenant_context(tenant_a):
        order = await _claimed_order(fnb.id, waiter)
        await mark_request_paid(order.id, acting_user=waiter)
        with pytest.raises(AppError):
            await mark_request_paid(order.id, acting_user=waiter)
        rows = [row for row in await _journal() if row.request_id == order.id]

    marks = [row for row in rows if row.action is StaffAction.REQUEST_PAYMENT_MARKED]
    assert len(marks) == 1
    assert marks[0].actor_user_id == waiter.user_id
    assert marks[0].request_daily_number == order.daily_number
    assert marks[0].category_name == "F&B"
    assert marks[0].category_id == fnb.id
    assert marks[0].room_number == "512"


@pytest.mark.parametrize("state", ["new", "no_payment", "already_paid", "done", "cancelled"])
async def test_inapplicable_payment_mark_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID], state: str
) -> None:
    """008: не взята, без признака, уже оплачена, закрыта. Повторная отметка не
    перезаписывает, кто и когда взял деньги; события отметки отказ не оставляет."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    waiter = await make_acting_user("Айгерим")
    other = await make_acting_user("Ержан")
    with tenant_context(tenant_a):
        if state == "no_payment":
            plain = await create_request(
                ServiceRequestCreate(
                    category_id=fnb.id, origin=ServiceRequestOrigin.GUEST_CHAT, summary="Чай"
                )
            )
            target = await change_request_status(plain.id, RequestStatus.IN_PROGRESS)
        elif state == "new":
            target = await _order(fnb.id)
        else:
            target = await _claimed_order(fnb.id, waiter)
            if state in ("already_paid", "done"):
                await mark_request_paid(target.id, acting_user=waiter)
            if state == "done":
                await change_request_status(target.id, RequestStatus.DONE)
            if state == "cancelled":
                await change_request_status(target.id, RequestStatus.CANCELLED)
        before = await get_request(target.id)
        marks_before = _event_names(await _outbox(target.id)).count("request.payment_marked")
        with pytest.raises(AppError) as error:
            await mark_request_paid(target.id, acting_user=other)
        after = await get_request(target.id)

    assert error.value.code == ERR_REQUESTS_PAYMENT_NOT_APPLICABLE
    assert error.value.status_code == 409
    assert after == before
    assert _event_names(await _outbox(target.id)).count("request.payment_marked") == marks_before


async def test_payment_mark_and_its_event_share_one_transaction(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-6: публикация упала — отметки нет. Отметка без события оставила бы гостя
    без «Оплата получена», событие без отметки — сообщило бы неправду."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    waiter = await make_acting_user("Айгерим")

    async def failing_publish(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("outbox down")

    with tenant_context(tenant_a):
        order = await _claimed_order(fnb.id, waiter)
        monkeypatch.setattr("hospitality.modules.requests.payment.publish", failing_publish)
        with pytest.raises(RuntimeError):
            await mark_request_paid(order.id, acting_user=waiter)
        after = await get_request(order.id)

    assert after.paid_at is None
    assert after.paid_by_display_name is None


async def test_payment_mark_keeps_the_service_boundary(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Ревью PR #497 Н-4: граница службы — в домене, как у перехода (007)."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    supply = await make_category(tenant_a, key="supply", name="Снабжение")
    waiter = await make_acting_user("Айгерим")
    with tenant_context(tenant_a):
        order = await _claimed_order(fnb.id, waiter)
        with pytest.raises(AppError) as error:
            await mark_request_paid(order.id, acting_user=waiter, allowed_category_ids={supply.id})
        after = await get_request(order.id)

    assert error.value.code == ERR_REQUESTS_FOREIGN_SERVICE
    assert after.paid_at is None


async def test_payment_mark_does_not_reach_another_tenant(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """P-4: заказ отеля A из контекста отеля B не найден (002) и не изменён."""
    tenant_a, tenant_b = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    waiter = await make_acting_user("Айгерим")
    with tenant_context(tenant_a):
        order = await _claimed_order(fnb.id, waiter)
    with tenant_context(tenant_b), pytest.raises(AppError) as error:
        await mark_request_paid(order.id, acting_user=waiter)
    with tenant_context(tenant_a):
        after = await get_request(order.id)

    assert error.value.code == ERR_REQUESTS_REQUEST_NOT_FOUND
    assert after.paid_at is None


async def test_guest_cannot_cancel_a_paid_order(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """008: деньги у сотрудника — гость не отменит ни взятую, ни возвращённую в
    очередь (там `only_from=new` пройдёт, поэтому защита — по отметке). Сотрудник
    оплаченную отменить может: возврат денег — касса отеля."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    waiter = await make_acting_user("Айгерим")
    with tenant_context(tenant_a):
        order = await _claimed_order(fnb.id, waiter)
        await mark_request_paid(order.id, acting_user=waiter)
        with pytest.raises(AppError) as from_work:
            await change_request_status(
                order.id, RequestStatus.CANCELLED, initiator=RequestInitiator.GUEST
            )
        await change_request_status(order.id, RequestStatus.NEW, acting_user=waiter)
        with pytest.raises(AppError) as from_queue:
            await change_request_status(
                order.id,
                RequestStatus.CANCELLED,
                initiator=RequestInitiator.GUEST,
                only_from=RequestStatus.NEW,
            )
        returned = await get_request(order.id)
        cancelled = await change_request_status(
            order.id, RequestStatus.CANCELLED, initiator=RequestInitiator.STAFF
        )

    assert from_work.value.code == ERR_REQUESTS_PAYMENT_NOT_APPLICABLE
    assert from_queue.value.code == ERR_REQUESTS_PAYMENT_NOT_APPLICABLE
    assert returned.status is RequestStatus.NEW
    assert returned.paid_at is not None, "возврат в очередь отметку не снимает (Р-4)"
    assert cancelled.status is RequestStatus.CANCELLED


async def test_request_without_payment_flag_closes_as_before(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Приёмка PR 2: заявки без признака ведут себя как раньше — `done` без отметки."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=housekeeping.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="Полотенца",
            )
        )
        await change_request_status(request.id, RequestStatus.IN_PROGRESS)
        done = await change_request_status(request.id, RequestStatus.DONE)

    assert done.status is RequestStatus.DONE
    assert done.payment_required is False
    assert done.paid_at is None


async def test_database_rejects_a_payment_mark_without_the_flag(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """CHECK `paid_at IS NULL OR payment_required` (§3): мимо домена тоже не пройдёт."""
    tenant_a, _ = two_tenants
    housekeeping = await make_category(tenant_a)
    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=housekeeping.id, origin=ServiceRequestOrigin.GUEST_CHAT, summary="Чай"
            )
        )
        with pytest.raises(IntegrityError, match="ck_service_requests_paid_requires_payment"):
            async with session_scope() as session:
                await session.execute(
                    update(ServiceRequest)
                    .where(ServiceRequest.id == request.id)
                    .values(paid_at=request.created_at)
                )


async def test_same_request_id_gives_one_request_and_one_event(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """P-8, Р-3: повтор «Оформить» с тем же id — та же заявка, без второго события."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    request_id = uuid.uuid4()
    with tenant_context(tenant_a):
        first = await _order(fnb.id, request_id=request_id)
        second = await _order(fnb.id, request_id=request_id)
        async with session_scope() as session:
            rows = list(await session.scalars(select(ServiceRequest.id)))

    assert first.id == second.id == request_id
    assert second.daily_number == first.daily_number
    assert rows == [request_id]
    assert _event_names(await _outbox(request_id)) == ["request.created"]


async def test_parallel_retry_with_same_request_id_returns_the_existing_request(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Гонка двух повторов: оба не нашли заявку, второй упёрся в первичный ключ —
    и вернул существующую, а не 500. Чередование задано: проверка «уже есть?»
    у второго подменена на «нет», как если бы он прочёл до чужого commit'а."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    request_id = uuid.uuid4()
    real_lookup = requests_service._existing_request
    lookups: list[uuid.UUID] = []

    async def stale_first_lookup(looked_up: uuid.UUID) -> ServiceRequestRead | None:
        lookups.append(looked_up)
        return None if len(lookups) == 1 else await real_lookup(looked_up)

    with tenant_context(tenant_a):
        first = await _order(fnb.id, request_id=request_id)
        monkeypatch.setattr(requests_service, "_existing_request", stale_first_lookup)
        second = await _order(fnb.id, request_id=request_id)

    assert lookups == [request_id, request_id], "второй вызов не дошёл до первичного ключа"
    assert second.id == first.id
    assert _event_names(await _outbox(request_id)) == ["request.created"]


async def test_only_from_rejects_a_request_in_another_status(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """`only_from`: отмена «только из new» по взятой заявке — 003, заявка в работе."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    with tenant_context(tenant_a):
        order = await _order(fnb.id)
        await change_request_status(order.id, RequestStatus.IN_PROGRESS)
        with pytest.raises(AppError) as error:
            await change_request_status(
                order.id,
                RequestStatus.CANCELLED,
                initiator=RequestInitiator.GUEST,
                only_from=RequestStatus.NEW,
            )
        after = await get_request(order.id)

    assert error.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION
    assert after.status is RequestStatus.IN_PROGRESS


async def test_guest_cancel_and_staff_claim_race_has_one_winner(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Сотрудник берёт заказ и держит строку, отмена гостем «только из new» уже
    ждёт её. После коммита взятия отмена видит `in_progress` — 003, заказ в
    работе. Сверка до блокировки прочла бы `new` и отменила бы взятый заказ."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    with tenant_context(tenant_a):
        order = await _order(fnb.id)

        async def guest_cancel() -> None:
            await change_request_status(
                order.id,
                RequestStatus.CANCELLED,
                initiator=RequestInitiator.GUEST,
                only_from=RequestStatus.NEW,
            )

        async with session_scope() as claim_session:
            row = (
                await claim_session.execute(
                    select(ServiceRequest).where(ServiceRequest.id == order.id).with_for_update()
                )
            ).scalar_one()
            row.status = RequestStatus.IN_PROGRESS
            await claim_session.flush()
            cancel = asyncio.create_task(guest_cancel())
            blocked = await _wait_until_a_backend_blocks()
        # Выход из scope — коммит взятия: отмена просыпается здесь.
        with pytest.raises(AppError) as error:
            await cancel
        after = await get_request(order.id)

    assert blocked, "отмена не встала на блокировку — чередования не было"
    assert error.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION
    assert after.status is RequestStatus.IN_PROGRESS


async def test_concurrent_payment_marks_have_one_winner(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Две вкладки жмут «Оплачено» разом (ревью PR #498, Н-2): первая держит строку,
    вторая ждёт её. После коммита первой вторая видит отметку — 008, «кто взял
    деньги» не перезаписан. Чтение без `FOR UPDATE` прочло бы строку до коммита и
    поставило бы отметку второй раз."""
    tenant_a, _ = two_tenants
    fnb = await make_category(tenant_a, key="fnb", name="F&B")
    waiter = await make_acting_user("Айгерим")
    runner = await make_acting_user("Ержан")
    with tenant_context(tenant_a):
        order = await _claimed_order(fnb.id, waiter)

        async def second_mark() -> None:
            await mark_request_paid(order.id, acting_user=runner)

        async with session_scope() as first_session:
            row = (
                await first_session.execute(
                    select(ServiceRequest).where(ServiceRequest.id == order.id).with_for_update()
                )
            ).scalar_one()
            row.paid_at = row.claimed_at
            row.paid_by_user_id = waiter.user_id
            row.paid_by_display_name = waiter.display_name
            await first_session.flush()
            mark = asyncio.create_task(second_mark())
            blocked = await _wait_until_a_backend_blocks()
        with pytest.raises(AppError) as error:
            await mark
        after = await get_request(order.id)

    assert blocked, "вторая отметка не встала на блокировку — чередования не было"
    assert error.value.code == ERR_REQUESTS_PAYMENT_NOT_APPLICABLE
    assert after.paid_by_display_name == "Айгерим"
