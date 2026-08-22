"""Жизненный цикл заявки (Task 0012, §5.2): создание, валидные и невалидные
переходы статусов, ошибки с кодами каталога.

Весь доступ — через публичный интерфейс `api.py`, как у настоящего
потребителя модуля (HTTP API, AI-инструмент).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    ERR_REQUESTS_CATEGORY_NOT_FOUND,
    ERR_REQUESTS_INVALID_STATUS_TRANSITION,
    ERR_REQUESTS_REQUEST_NOT_FOUND,
    ActingUser,
    RequestCategoryCreate,
    RequestInitiator,
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    anonymize_expired_request_texts,
    change_request_status,
    create_category,
    create_request,
    get_request,
    list_open_requests,
    list_open_requests_by_ids,
    list_requests_closed_since,
    list_unclaimed_requests,
)
from hospitality.modules.requests.models import ServiceRequest
from hospitality.modules.requests.tests.conftest import make_category
from hospitality.platform.models import User
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


async def make_acting_user(display_name: str) -> ActingUser:
    """Платформенный User для acting_user: колонка claimed_by_user_id — FK на
    `users` (миграция 0018), случайный uuid БД не пропустит."""
    async with platform_session_scope() as session:
        user = User(display_name=display_name)
        session.add(user)
        await session.flush()
        return ActingUser(user_id=user.id, display_name=display_name)


async def test_request_is_created_in_status_new(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """DoD задачи: заявку можно создать вызовом сервиса в тесте."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
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
    assert request.is_urgent is False  # умолчание — обычная заявка (spec 0034 §5)


async def test_urgent_flag_is_stored_and_returned(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Признак срочности — свойство заявки, а не статус (spec 0034 §5).

    Домен на него не смотрит: гейт подтверждения снимает AI-слой (ADR-018),
    ночную доставку ветвит канал (issue #212). Модуль обязан лишь честно
    сохранить и вернуть.
    """
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="течёт вода с потолка",
                is_urgent=True,
            )
        )
        stored = await get_request(request.id)

    assert request.is_urgent is True
    assert stored.is_urgent is True


async def test_create_request_with_unknown_category_fails(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await create_request(
            ServiceRequestCreate(
                category_id=uuid.uuid4(),
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="no such category",
            )
        )
    assert error.value.code == ERR_REQUESTS_CATEGORY_NOT_FOUND
    assert error.value.status_code == 404


async def test_full_lifecycle_new_in_progress_done(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Полный путь ADR-013: new → in_progress → done (без assigned)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="Fix the shower",
            )
        )
        for expected_status in (
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
        (RequestStatus.IN_PROGRESS,),
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
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="cancel me"
            )
        )
        for status in start_status_path:
            await change_request_status(request.id, status)
        cancelled = await change_request_status(request.id, RequestStatus.CANCELLED)
    assert cancelled.status is RequestStatus.CANCELLED


@pytest.mark.parametrize(
    ("status_path", "invalid_target"),
    [
        ((), RequestStatus.DONE),  # new → done, минуя работу
        ((), RequestStatus.NEW),  # переход «в тот же статус»
        # Терминальные статусы: из done и cancelled пути нет.
        (
            (RequestStatus.IN_PROGRESS, RequestStatus.DONE),
            RequestStatus.IN_PROGRESS,
        ),
        ((RequestStatus.CANCELLED,), RequestStatus.IN_PROGRESS),
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
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="strict lifecycle",
            )
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


async def test_list_open_requests_by_ids_returns_open_own_in_creation_order(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0025 (опора снапшота диалога): среди переданных id возвращаются только
    ОТКРЫТЫЕ заявки своего тенанта в порядке создания; терминальные, чужие
    тенанту (RLS) и несуществующие id молча выпадают."""
    tenant_a, tenant_b = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        first = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="one"
            )
        )
        second = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="two"
            )
        )
        await change_request_status(second.id, RequestStatus.IN_PROGRESS)
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
        await change_request_status(cancelled.id, RequestStatus.CANCELLED)

        all_ids = [first.id, second.id, done.id, cancelled.id, uuid.uuid4()]
        open_requests = await list_open_requests_by_ids(all_ids)
        assert [r.id for r in open_requests] == [first.id, second.id]
        assert await list_open_requests_by_ids([]) == []

    # Чужой тенант с теми же id не видит ничего (RLS, P-4) — «чужую не увидеть».
    with tenant_context(tenant_b):
        assert await list_open_requests_by_ids(all_ids) == []


async def test_change_status_of_missing_request_fails(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a), pytest.raises(AppError) as error:
        await change_request_status(uuid.uuid4(), RequestStatus.IN_PROGRESS)
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


async def test_resolution_note_saved_on_terminal_transition(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0021 П-4: примечание закрытия пишется на терминальном переходе (done)
    и обрезается по краям; итог виден и в снимке, и в хранилище."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="убрать 305",
            )
        )
        await change_request_status(request.id, RequestStatus.IN_PROGRESS)
        done = await change_request_status(
            request.id, RequestStatus.DONE, resolution_note="  кофе закончился  "
        )
        stored = await get_request(request.id)
    assert done.resolution_note == "кофе закончился"
    assert stored.resolution_note == "кофе закончился"


async def test_resolution_note_ignored_on_non_terminal_transition(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0021 П-4: на нетерминальном переходе (new → in_progress) примечанию
    некуда «закрыться» — оно игнорируется (README), заявка остаётся без него.

    Контракт `_apply_transition`/staff.py опирается на это: примечание не
    расширяет карту переходов и не «протекает» на промежуточный статус.
    """
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="убрать 305",
            )
        )
        started = await change_request_status(
            request.id, RequestStatus.IN_PROGRESS, resolution_note="рано ещё"
        )
        stored = await get_request(request.id)
    assert started.resolution_note is None
    assert stored.resolution_note is None


async def test_list_unclaimed_requests_returns_only_new_older_than_cutoff(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0028 (опора напоминаний): только `new` и только созданные раньше
    границы; новые сверху, `limit` уважается; чужие тенанту не видны (RLS)."""
    tenant_a, tenant_b = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        older = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="one"
            )
        )
        newer = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="two"
            )
        )
        claimed = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="claimed"
            )
        )
        await change_request_status(claimed.id, RequestStatus.IN_PROGRESS)
        cutoff = utc_now()
        # Заявка моложе границы: она ещё не «висит».
        await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="fresh"
            )
        )

        unclaimed = await list_unclaimed_requests(created_before=cutoff, limit=10)
        assert [request.id for request in unclaimed] == [newer.id, older.id]  # новые сверху
        assert all(request.status is RequestStatus.NEW for request in unclaimed)

        # Срез — страховка от неограниченного скана: при limit=1 остаётся самая
        # свежая просроченная (та, которой ещё не напоминали).
        assert [
            request.id for request in await list_unclaimed_requests(created_before=cutoff, limit=1)
        ] == [newer.id]

    with tenant_context(tenant_b):
        assert await list_unclaimed_requests(created_before=cutoff, limit=10) == []


async def test_claim_with_acting_user_writes_claimed_by(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0033 §5/§10: «взять» с acting_user пишет id и снапшот имени; на
    терминальном переходе колонки не переписываются (история взятия честна)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    actor = await make_acting_user("Санжар Техник")
    closer = await make_acting_user("Аружан Менеджер")

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="убрать 305",
            )
        )
        assert request.claimed_by_user_id is None
        assert request.claimed_by_display_name is None

        claimed = await change_request_status(
            request.id, RequestStatus.IN_PROGRESS, acting_user=actor
        )
        assert claimed.claimed_by_user_id == actor.user_id
        assert claimed.claimed_by_display_name == "Санжар Техник"

        # Закрыл другой сотрудник — «кто взял» не переписывается (v1 не хранит
        # «кто закрыл»; acting_user действует только на new → in_progress).
        done = await change_request_status(request.id, RequestStatus.DONE, acting_user=closer)
        assert done.claimed_by_user_id == actor.user_id
        assert done.claimed_by_display_name == "Санжар Техник"


async def test_claim_without_acting_user_leaves_columns_empty(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0033 §10: Telegram-путь (и HTTP API) не несёт личности — переходы
    без acting_user оставляют claimed_by пустым, деградация невидима."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="убрать 305",
            )
        )
        claimed = await change_request_status(request.id, RequestStatus.IN_PROGRESS)
        stored = await get_request(request.id)
    assert claimed.claimed_by_user_id is None
    assert claimed.claimed_by_display_name is None
    assert stored.claimed_by_user_id is None
    assert stored.claimed_by_display_name is None


async def test_acting_user_on_cancel_from_new_does_not_claim(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Отмена сразу из `new` — не «взятие»: колонки claimed_by остаются пустыми."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="отменить"
            )
        )
        cancelled = await change_request_status(
            request.id,
            RequestStatus.CANCELLED,
            resolution_note="гость передумал",
            acting_user=await make_acting_user("Аружан"),
        )
    assert cancelled.claimed_by_user_id is None
    assert cancelled.claimed_by_display_name is None


async def test_repeated_claim_is_invalid_transition(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0033 §10: повторное «взять» (in_progress → in_progress) → 409
    ERR-REQUESTS-003; первый взявший не затирается (кабинет покажет «уже взята»)."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    first = await make_acting_user("Первый")
    second = await make_acting_user("Второй")

    with tenant_context(tenant_a):
        request = await create_request(
            ServiceRequestCreate(
                category_id=category.id,
                origin=ServiceRequestOrigin.GUEST_CHAT,
                summary="убрать 305",
            )
        )
        await change_request_status(request.id, RequestStatus.IN_PROGRESS, acting_user=first)
        with pytest.raises(AppError) as error:
            await change_request_status(request.id, RequestStatus.IN_PROGRESS, acting_user=second)
        stored = await get_request(request.id)
    assert error.value.code == ERR_REQUESTS_INVALID_STATUS_TRANSITION
    assert error.value.status_code == 409
    assert stored.claimed_by_user_id == first.user_id
    assert stored.claimed_by_display_name == "Первый"


async def test_list_open_requests_returns_open_newest_first(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0033 §5 (лента очереди): только new + in_progress, новые сверху,
    limit уважается; чужие тенанту не видны (RLS)."""
    tenant_a, tenant_b = two_tenants
    category = await make_category(tenant_a)

    with tenant_context(tenant_a):
        first = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="one"
            )
        )
        second = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="two"
            )
        )
        await change_request_status(second.id, RequestStatus.IN_PROGRESS)
        closed = await create_request(
            ServiceRequestCreate(
                origin=ServiceRequestOrigin.GUEST_CHAT, category_id=category.id, summary="closed"
            )
        )
        await change_request_status(closed.id, RequestStatus.IN_PROGRESS)
        await change_request_status(closed.id, RequestStatus.DONE)

        open_requests = await list_open_requests(limit=10)
        assert [request.id for request in open_requests] == [second.id, first.id]
        assert [request.id for request in await list_open_requests(limit=1)] == [second.id]

    with tenant_context(tenant_b):
        assert await list_open_requests(limit=10) == []


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
        # Свежезакрытые сверху (updated_at DESC).
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


# ---------------------------------------------------------------------------
# Метки времени и «кто закрыл» (spec 0035 §3, §13 — блок «Метки времени»)
# ---------------------------------------------------------------------------


async def read_closed_by(request_id: uuid.UUID) -> tuple[uuid.UUID | None, str | None]:
    """Пара `closed_by_*` прямо из строки БД.

    Наружу модуля эти колонки не отдаются намеренно (spec 0035 §13:
    `ServiceRequestRead` несёт только «когда» и «чем»), поэтому тест читает их
    из ORM — вызывается уже внутри `tenant_context`, RLS показывает свою строку.
    """
    async with session_scope() as session:
        row = (
            await session.execute(select(ServiceRequest).where(ServiceRequest.id == request_id))
        ).scalar_one()
        return (row.closed_by_user_id, row.closed_by_display_name)


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
