"""Ретеншн гостевых текстов (issue #42, spec 0032).

Проверяет прогон целиком (`enforce_guest_text_retention`) — так его зовёт
воркер: удаление старых сообщений и опустевших диалогов, обезличивание текстов
заявок, обход всех тенантов, изоляция сбоев.

Возраст строк задаётся сдвигом `created_at`/`updated_at` SQL'ом (канон
test_reminders): ждать реальные дни нельзя, а подменять «сейчас» — значит
проверять не тот код, который поедет. RLS ограничивает UPDATE текущим тенантом.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from hospitality.channels.common import retention
from hospitality.channels.common.models import (
    Conversation,
    Message,
    MessageContentKind,
    MessageDirection,
    RequestOrigin,
)
from hospitality.channels.common.retention import enforce_guest_text_retention
from hospitality.channels.common.store import ensure_conversation, record_request_origin
from hospitality.modules.requests import api as requests_api
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.tenancy import tenant_context

RETENTION_DAYS = 90
OLD = 91  # дней: за границей срока
FRESH = 1  # дней: внутри срока


def _days_ago(days: int) -> datetime:
    return utc_now() - timedelta(days=days)


async def _make_conversation(
    tenant_id: uuid.UUID,
    *,
    external_id: str,
    updated_days_ago: int,
    message_ages_days: tuple[int, ...] = (),
) -> uuid.UUID:
    """Диалог тенанта с сообщениями заданного возраста; вернуть его id.

    Пишется каноническим путём (`ensure_conversation` + INSERT сообщений через
    ORM-модель — тестам общего ядра свои модели доступны), возраст двигается
    SQL'ом после записи.
    """
    with tenant_context(tenant_id):
        conversation_id = await ensure_conversation("telegram", external_id)
        async with session_scope() as session:
            for age_days in message_ages_days:
                session.add(
                    Message(
                        conversation_id=conversation_id,
                        direction=MessageDirection.INBOUND,
                        content_kind=MessageContentKind.TEXT,
                        text=f"реплика {age_days} дн назад",
                        correlation_id="test",
                    )
                )
            await session.flush()
        # Возраст каждого сообщения — отдельным UPDATE по тексту-маркеру.
        async with session_scope() as session:
            for age_days in message_ages_days:
                await session.execute(
                    text(
                        "UPDATE messages SET created_at = :ts "
                        "WHERE conversation_id = :cid AND text = :marker"
                    ),
                    {
                        "ts": _days_ago(age_days),
                        "cid": conversation_id,
                        "marker": f"реплика {age_days} дн назад",
                    },
                )
            await session.execute(
                text("UPDATE conversations SET updated_at = :ts WHERE id = :cid"),
                {"ts": _days_ago(updated_days_ago), "cid": conversation_id},
            )
    return conversation_id


async def _count(tenant_id: uuid.UUID, model: type) -> int:
    with tenant_context(tenant_id):
        async with session_scope() as session:
            return (await session.scalar(select(func.count()).select_from(model))) or 0


async def _make_request(
    tenant_id: uuid.UUID, *, age_days: int, done_with_note: str | None = None
) -> requests_api.ServiceRequestRead:
    """Заявка тенанта, «созданная» age_days назад (канон test_reminders)."""
    with tenant_context(tenant_id):
        categories = {category.key: category for category in await requests_api.list_categories()}
        category = categories.get("housekeeping")
        if category is None:
            category = await requests_api.create_category(
                requests_api.RequestCategoryCreate(key="housekeeping", name="Уборка")
            )
        request = await requests_api.create_request(
            requests_api.ServiceRequestCreate(
                category_id=category.id,
                summary="нужны полотенца",
                details="гость просил два комплекта",
                room_number="305",
            )
        )
        if done_with_note is not None:
            await requests_api.change_request_status(
                request.id, requests_api.RequestStatus.IN_PROGRESS
            )
            await requests_api.change_request_status(
                request.id, requests_api.RequestStatus.DONE, resolution_note=done_with_note
            )
        async with session_scope() as session:
            await session.execute(
                text("UPDATE service_requests SET created_at = :ts WHERE id = :id"),
                {"ts": _days_ago(age_days), "id": request.id},
            )
        return request


async def test_old_messages_are_deleted_fresh_are_kept(demo_tenant: uuid.UUID) -> None:
    """DoD issue #42: сообщения старше срока удаляются, свежие — нет."""
    await _make_conversation(
        demo_tenant, external_id="1", updated_days_ago=FRESH, message_ages_days=(OLD, FRESH)
    )

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.messages_deleted == 1
    assert await _count(demo_tenant, Message) == 1  # свежая реплика жива


async def test_stale_empty_conversation_is_deleted_with_request_origin(
    demo_tenant: uuid.UUID,
) -> None:
    """Пустой давно не обновлявшийся диалог уходит; каскадом — его привязка
    заявки (`request_origins`); согласие — доказательная запись «как у диалога»
    (PII_REGISTRY) — умирает вместе с ним."""
    conversation_id = await _make_conversation(demo_tenant, external_id="1", updated_days_ago=OLD)
    with tenant_context(demo_tenant):
        await record_request_origin(uuid.uuid4(), conversation_id)

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.conversations_deleted == 1
    assert await _count(demo_tenant, Conversation) == 0
    assert await _count(demo_tenant, RequestOrigin) == 0


async def test_conversation_with_fresh_messages_survives(demo_tenant: uuid.UUID) -> None:
    """Живой диалог не трогается: свежие сообщения держат его, даже если сам
    `updated_at` старый (запись сообщений его не обновляет)."""
    await _make_conversation(
        demo_tenant, external_id="1", updated_days_ago=OLD, message_ages_days=(FRESH,)
    )

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.conversations_deleted == 0
    assert await _count(demo_tenant, Conversation) == 1


async def test_recently_updated_empty_conversation_survives(demo_tenant: uuid.UUID) -> None:
    """Пустой, но недавно обновлённый диалог (pending_action, только что данное
    согласие) — жив: `updated_at` внутри срока."""
    await _make_conversation(demo_tenant, external_id="1", updated_days_ago=FRESH)

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.conversations_deleted == 0


async def test_conversation_emptied_by_same_run_is_deleted(demo_tenant: uuid.UUID) -> None:
    """Диалог, чьё последнее сообщение удалено этим же прогоном, уходит сразу
    (spec 0032 §2: порядок «сообщения → диалоги» внутри тенанта)."""
    await _make_conversation(
        demo_tenant, external_id="1", updated_days_ago=OLD, message_ages_days=(OLD, OLD)
    )

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.messages_deleted == 2
    assert stats.conversations_deleted == 1
    assert await _count(demo_tenant, Conversation) == 0


async def test_old_request_texts_are_anonymized_aggregates_survive(
    demo_tenant: uuid.UUID,
) -> None:
    """Свободный текст старой заявки обезличен, агрегаты для отчётов целы
    (решение основателя в issue #42: не удалять — анонимизировать)."""
    request = await _make_request(demo_tenant, age_days=OLD, done_with_note="выдал один комплект")

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.requests_anonymized == 1
    with tenant_context(demo_tenant):
        stored = await requests_api.get_request(request.id)
    assert stored.summary == requests_api.REQUEST_TEXT_ANONYMIZED_PLACEHOLDER
    assert stored.details is None
    assert stored.resolution_note is None
    # Отчёты живут: статус, категория, комната, дневной номер не тронуты.
    assert stored.status is requests_api.RequestStatus.DONE
    assert stored.category_id == request.category_id
    assert stored.room_number == "305"
    assert stored.daily_number == request.daily_number


async def test_fresh_request_texts_are_kept(demo_tenant: uuid.UUID) -> None:
    """Свежая заявка нетронута — обезличивание только за границей срока; статус
    роли не играет (обезличивается и открытая, если стара)."""
    fresh = await _make_request(demo_tenant, age_days=FRESH)
    stale_open = await _make_request(demo_tenant, age_days=OLD)

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.requests_anonymized == 1
    with tenant_context(demo_tenant):
        assert (await requests_api.get_request(fresh.id)).summary == "нужны полотенца"
        stored_stale = await requests_api.get_request(stale_open.id)
    assert stored_stale.summary == requests_api.REQUEST_TEXT_ANONYMIZED_PLACEHOLDER
    assert stored_stale.status is requests_api.RequestStatus.NEW


async def test_run_is_idempotent(demo_tenant: uuid.UUID) -> None:
    """P-8: повторный прогон не находит работы — все счётчики нулевые."""
    await _make_conversation(
        demo_tenant, external_id="1", updated_days_ago=OLD, message_ages_days=(OLD,)
    )
    await _make_request(demo_tenant, age_days=OLD)

    first = await enforce_guest_text_retention(RETENTION_DAYS)
    second = await enforce_guest_text_retention(RETENTION_DAYS)

    assert first.messages_deleted == 1
    assert second == retention.RetentionRunStats(
        tenants=1, messages_deleted=0, conversations_deleted=0, requests_anonymized=0
    )


async def test_all_tenants_are_walked_even_without_config(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """P-4 + spec 0032 §3: прогон обходит всех тенантов (оба без конфига —
    онбординг не завершён), работая с каждым под его `tenant_context`; свежие
    данные соседа не задеты."""
    tenant_a, tenant_b = two_tenants
    await _make_conversation(
        tenant_a, external_id="a", updated_days_ago=OLD, message_ages_days=(OLD,)
    )
    await _make_conversation(
        tenant_b, external_id="b", updated_days_ago=FRESH, message_ages_days=(FRESH,)
    )

    stats = await enforce_guest_text_retention(RETENTION_DAYS)

    assert stats.tenants == 2
    assert stats.messages_deleted == 1
    assert await _count(tenant_a, Message) == 0
    assert await _count(tenant_b, Message) == 1


async def test_failure_on_one_tenant_does_not_stop_the_others(
    two_tenants: tuple[uuid.UUID, uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой на одном отеле логируется ERR-CHANNEL-004 и не отменяет ретеншн у
    остальных: обещание политики дано каждому гостю."""
    broken, healthy = two_tenants
    await _make_conversation(
        healthy, external_id="h", updated_days_ago=OLD, message_ages_days=(OLD,)
    )

    original = retention._enforce_tenant

    async def failing_for_broken(
        tenant_id: uuid.UUID, cutoff: datetime
    ) -> retention.RetentionRunStats:
        if tenant_id == broken:
            raise RuntimeError("connection reset")
        return await original(tenant_id, cutoff)

    monkeypatch.setattr(retention, "_enforce_tenant", failing_for_broken)

    stats = await enforce_guest_text_retention(RETENTION_DAYS)  # не бросает

    assert stats.messages_deleted == 1
    assert await _count(healthy, Message) == 0
