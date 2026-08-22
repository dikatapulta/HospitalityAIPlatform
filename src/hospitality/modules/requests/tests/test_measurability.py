"""Метки времени и «кто закрыл» (spec 0035 §3, issue #298).

Главная асимметрия модуля живёт здесь, а не в `test_lifecycle.py`: **время**
перехода пишется из любого канала, **имя** — только там, где есть личность
(`acting_user` кабинета). Отдельный файл потому, что `test_lifecycle.py` и без
этого блока перерос границу R-3 (~400 строк), а блок самодостаточен.

Что стало с уже существовавшими строками при выкатке колонок — не здесь:
бэкфилл проверяет `tests/test_migration_0025_measurability.py` на настоящей
до-миграционной БД.
"""

from __future__ import annotations

import uuid

from hospitality.modules.requests.api import (
    RequestInitiator,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    change_request_status,
    create_request,
)
from hospitality.modules.requests.tests.conftest import (
    make_acting_user,
    make_category,
    read_closed_by,
)
from hospitality.shared.tenancy import tenant_context


async def test_claim_writes_claimed_at_from_every_channel(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §3: время взятия пишется на КАЖДОМ переходе new → in_progress —
    и из кабинета (`acting_user`), и из Telegram (без него). Имя — только там,
    где личность есть: иначе медиана времени взятия считалась бы по одному
    каналу и молча занижала объём работы, сделанной через другой."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    maria = await make_acting_user("Мария")

    with tenant_context(tenant_a):
        from_cabinet = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="из кабинета",
            )
        )
        from_telegram = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="из телеграма",
            )
        )
        assert from_cabinet.claimed_at is None  # до взятия — пусто у обеих
        assert from_telegram.claimed_at is None

        cabinet = await change_request_status(
            from_cabinet.id, RequestStatus.IN_PROGRESS, acting_user=maria
        )
        telegram = await change_request_status(from_telegram.id, RequestStatus.IN_PROGRESS)

    assert cabinet.claimed_at is not None
    assert cabinet.claimed_by_display_name == "Мария"
    # Главное утверждение: канал без личности всё равно оставил метку времени.
    assert telegram.claimed_at is not None
    assert telegram.claimed_by_user_id is None
    assert telegram.claimed_by_display_name is None


async def test_close_writes_closed_at_always_and_name_only_with_acting_user(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §3: `closed_at` пишется на каждом терминальном переходе,
    `closed_by_*` — только когда личность известна (`acting_user` кабинета)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    ivan = await make_acting_user("Иван")

    with tenant_context(tenant_a):
        by_cabinet = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="закроет кабинет",
            )
        )
        by_telegram = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="закроет телеграм",
            )
        )
        await change_request_status(by_cabinet.id, RequestStatus.IN_PROGRESS, acting_user=ivan)
        await change_request_status(by_telegram.id, RequestStatus.IN_PROGRESS)

        cabinet = await change_request_status(
            by_cabinet.id, RequestStatus.DONE, acting_user=ivan, resolution_note="убрано"
        )
        telegram = await change_request_status(by_telegram.id, RequestStatus.DONE)

        assert await read_closed_by(by_cabinet.id) == (ivan.user_id, "Иван")
        assert await read_closed_by(by_telegram.id) == (None, None)

    assert cabinet.closed_at is not None
    assert telegram.closed_at is not None
    # Предикат §3: имя закрывшего есть ⟹ время закрытия есть; обратное неверно.
    assert cabinet.closed_at >= cabinet.claimed_at  # type: ignore[operator]  # оба заполнены выше


async def test_guest_cancellation_writes_closed_at_without_name(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §3: отмена гостем (spec 0025) — «когда» известно, «кто» нет.

    Гость не сотрудник: `acting_user` этот путь не передаёт вовсе, поэтому
    в статистику отмен заявка попадает по времени, а имени у неё не появляется."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="передумал",
            )
        )
        cancelled = await change_request_status(
            request.id, RequestStatus.CANCELLED, initiator=RequestInitiator.GUEST
        )
        assert await read_closed_by(request.id) == (None, None)

    assert cancelled.closed_at is not None
    # Отменена невзятой: времени взятия не появилось (и приблизить его нечем).
    assert cancelled.claimed_at is None


async def test_non_terminal_transition_leaves_closed_at_empty(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """`closed_at` отвечает на «когда закрыли»: у взятой, но не закрытой заявки
    его нет — иначе «открыто сейчас» считалось бы по нему и врало (§3)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="в работе",
            )
        )
        in_progress = await change_request_status(request.id, RequestStatus.IN_PROGRESS)

    assert in_progress.claimed_at is not None
    assert in_progress.closed_at is None
