"""Метки времени и «кто закрыл» (spec 0035 §3, issue #298).

Главная асимметрия модуля живёт здесь, а не в `test_lifecycle.py`: **время**
перехода пишется из любого канала, **имя** — только там, где есть личность
(`acting_user` кабинета). Здесь же — вкладка «закрытые за сегодня», переведённая
этой же спекой с `updated_at` на `closed_at`: её предмет — тот же момент
закрытия. Отдельный файл потому, что `test_lifecycle.py` и без этого блока
перерос границу R-3 (~400 строк), а блок самодостаточен.

Что стало с уже существовавшими строками при выкатке колонок — не здесь:
бэкфилл проверяет `tests/test_migration_0025_measurability.py` на настоящей
до-миграционной БД.
"""

from __future__ import annotations

import uuid

from hospitality.modules.requests.api import (
    REQUEST_TEXT_ANONYMIZED_PLACEHOLDER,
    RequestInitiator,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    anonymize_expired_request_texts,
    change_request_status,
    create_request,
    get_request,
    list_requests_closed_since,
)
from hospitality.modules.requests.tests.conftest import (
    make_acting_user,
    make_category,
    read_closed_by,
)
from hospitality.shared.db import utc_now
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
    assert cabinet.claimed_at is not None
    assert cabinet.closed_at >= cabinet.claimed_at


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


async def test_list_requests_closed_since_by_closed_at(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0033 §5 (вкладка «закрытые за сегодня»): done/cancelled с
    `closed_at` не раньше границы; открытые и закрытые до границы не попадают."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        earlier = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="закрыта до границы",
            )
        )
        await change_request_status(earlier.id, RequestStatus.IN_PROGRESS)
        await change_request_status(earlier.id, RequestStatus.DONE)
        boundary = utc_now()

        done = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="done"
            )
        )
        await change_request_status(done.id, RequestStatus.IN_PROGRESS)
        await change_request_status(done.id, RequestStatus.DONE)
        cancelled = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="cancelled"
            )
        )
        await change_request_status(cancelled.id, RequestStatus.CANCELLED, resolution_note="дубль")
        await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="open"
            )
        )

        closed = await list_requests_closed_since(closed_after=boundary, limit=10)
        assert {request.id for request in closed} == {done.id, cancelled.id}
        # Свежезакрытые сверху (closed_at DESC).
        assert closed[0].id == cancelled.id


async def test_anonymized_requests_do_not_reappear_as_closed_today(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Находка ревью PR #154 после перехода на `closed_at` (spec 0035 §3).

    Обезличивание ретеншна (#42) по-прежнему бампает `updated_at`, но вкладка
    «закрытые за сегодня» смотрит теперь на момент закрытия, который джоба не
    трогает, — поэтому древняя заявка не всплывает уже по устройству, а не
    из-за отдельного условия по плейсхолдеру (оно этим PR снято)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        old = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="древняя заявка",
            )
        )
        await change_request_status(old.id, RequestStatus.CANCELLED, resolution_note="дубль")
        boundary = utc_now()

        # «Прогон джобы»: заявка старше порога (граница — будущее время) обезличена.
        assert await anonymize_expired_request_texts(created_before=utc_now()) == 1
        stored = await get_request(old.id)
        assert stored.updated_at >= boundary  # onupdate действительно бампнул

        assert await list_requests_closed_since(closed_after=boundary, limit=10) == []


async def test_long_open_request_anonymized_then_closed_shows_up_as_closed_today(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Обратная сторона перехода на `closed_at`, принятая намеренно.

    Джоба ретеншна (#42) обезличивает по возрасту и на статус не смотрит, так
    что заявка, провисевшая открытой дольше срока, а закрытая уже сегодня,
    придёт во вкладку с плейсхолдером вместо текста. Старое условие по
    плейсхолдеру прятало её целиком — то есть скрывало закрытие, которое
    действительно случилось сегодня, и расходилось бы со сводкой дня
    (spec 0035 §6: она считает закрытия по `closed_at`)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="висела дольше срока хранения",
            )
        )
        # «Прогон джобы» по ещё открытой заявке: границу задаёт вызывающая
        # сторона, статус джоба не смотрит.
        assert await anonymize_expired_request_texts(created_before=utc_now()) == 1
        boundary = utc_now()

        await change_request_status(request.id, RequestStatus.IN_PROGRESS)
        await change_request_status(request.id, RequestStatus.DONE)

        closed = await list_requests_closed_since(closed_after=boundary, limit=10)

    assert [row.id for row in closed] == [request.id]
    assert closed[0].summary == REQUEST_TEXT_ANONYMIZED_PLACEHOLDER
