"""Рассылка пуша: кому слать и о чём уже сообщили (spec 0050 §6, issue #477).

Данные прохода рассылки `staff_portal/push_sweep.py`; сама отправка — дверь
`staff_push.deliver`. Здесь:

- `load_recipients` — подписанные сотрудники отеля с живым членством; подписки
  отключённых, стёртых и ушедших из отеля удаляются тут же (spec 0050 §4);
- журнал `staff_push_announcements` (тенантная таблица, канон
  `TenantIsolationCanary`) — о чём сотруднику уже сообщили: серверная копия
  «уже прозвучало» вкладки (`signal.js` `compare()`). Проход записывает
  журнал целиком — в нём остаются только ключи того, что зовёт сейчас, поэтому
  ретеншн не нужен;
- `mark_primed` — подписка прошла первое включение.

Всё — в контексте тенанта (`tenant_context`): RLS режет подписки и журнал по
отелю, а членства и учётки (платформенный мир без RLS) сверяются по тому же
`tenant_id` явно. Ключ журнала — служебная строка без текста гостя.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import ForeignKey, String, and_, delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.platform.models import (
    MembershipStatus,
    Tenant,
    TenantMembership,
    User,
    UserStatus,
)
from hospitality.platform.staff_auth import StaffAudience
from hospitality.platform.staff_push import StaffPushSubscription
from hospitality.shared.db import Base, UTCDateTime, platform_session_scope, session_scope, utc_now
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

CALL_KEY_MAX_LENGTH: Final = 120


class StaffPushAnnouncement(Base):
    """О чём сотруднику уже сообщили на телефон (spec 0050 §6).

    Ключ — `new:<заявка>:<служба>`, `overdue:<заявка>`, `raised:<заявка>`,
    `call:<номер доски>:<шаг>`. Тенантная таблица по канону
    `TenantIsolationCanary` (P-4); первичный ключ начинается с `tenant_id`.
    """

    __tablename__ = "staff_push_announcements"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True, default=current_tenant_id
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    call_key: Mapped[str] = mapped_column(String(CALL_KEY_MAX_LENGTH), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


@dataclass(frozen=True)
class PushTenant:
    id: uuid.UUID
    slug: str


@dataclass(frozen=True)
class PushRecipient:
    """Сотрудник с телефонами: кого зовёт сигнал — `audience` (уровень и службы)."""

    user_id: uuid.UUID
    audience: StaffAudience
    subscriptions: tuple[StaffPushSubscription, ...]

    @property
    def primed(self) -> bool:
        """Хотя бы один телефон уже прошёл первое включение — журнал сотрудника живой."""
        return any(row.primed_at is not None for row in self.subscriptions)


async def list_push_tenants() -> list[PushTenant]:
    """Все отели реестра: подписки под RLS, и где они есть, видно только изнутри отеля."""
    async with platform_session_scope() as session:
        rows = await session.execute(
            select(Tenant.id, Tenant.slug).order_by(Tenant.created_at, Tenant.id)
        )
        return [PushTenant(id=tenant_id, slug=slug) for tenant_id, slug in rows]


async def load_recipients() -> tuple[list[PushRecipient], int]:
    """Подписанные сотрудники отеля с живым членством; второе — сколько подписок удалено.

    Подписка сотрудника, которого отключили, стёрли или вывели из отеля, удаляется:
    телефон ушедшего не зовёт (spec 0050 §4).
    """
    tenant_id = current_tenant_id()
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    StaffPushSubscription,
                    TenantMembership.role_key,
                    TenantMembership.service_keys,
                )
                .outerjoin(
                    TenantMembership,
                    and_(
                        TenantMembership.user_id == StaffPushSubscription.user_id,
                        TenantMembership.tenant_id == tenant_id,
                        TenantMembership.status == MembershipStatus.ACTIVE,
                    ),
                )
                .outerjoin(
                    User,
                    and_(
                        User.id == StaffPushSubscription.user_id, User.status == UserStatus.ACTIVE
                    ),
                )
                .add_columns(User.id)
                .order_by(StaffPushSubscription.user_id, StaffPushSubscription.updated_at.desc())
            )
        ).all()
        by_user: dict[uuid.UUID, list[StaffPushSubscription]] = {}
        audiences: dict[uuid.UUID, StaffAudience] = {}
        orphaned: list[uuid.UUID] = []
        for subscription, role_key, service_keys, active_user_id in rows:
            if role_key is None or active_user_id is None:
                orphaned.append(subscription.id)
                continue
            by_user.setdefault(subscription.user_id, []).append(subscription)
            audiences[subscription.user_id] = StaffAudience(
                tenant_id=tenant_id, role_key=role_key, service_keys=frozenset(service_keys)
            )
        if orphaned:
            await session.execute(
                delete(StaffPushSubscription).where(StaffPushSubscription.id.in_(orphaned))
            )
    if orphaned:
        logger.info("staff_push.inactive_member_subscriptions_deleted", count=len(orphaned))
    recipients = [
        PushRecipient(user_id=user_id, audience=audiences[user_id], subscriptions=tuple(subs))
        for user_id, subs in by_user.items()
    ]
    return recipients, len(orphaned)


async def load_announced() -> dict[uuid.UUID, frozenset[str]]:
    """Журнал отеля: о чём каждому сотруднику уже сообщили."""
    async with session_scope() as session:
        rows = await session.execute(
            select(StaffPushAnnouncement.user_id, StaffPushAnnouncement.call_key)
        )
        announced: dict[uuid.UUID, set[str]] = {}
        for user_id, call_key in rows:
            announced.setdefault(user_id, set()).add(call_key)
    return {user_id: frozenset(keys) for user_id, keys in announced.items()}


async def save_announced(
    previous: Mapping[uuid.UUID, frozenset[str]], current: Mapping[uuid.UUID, frozenset[str]]
) -> None:
    """Записать журнал прохода: у сотрудника из `current` — ровно эти ключи, у прочих — пусто.

    Пишется разница с `previous` (тем, что прочитал проход), а не журнал заново:
    на каждом проходе меняются единицы ключей, а проход — раз в 15 с.
    """
    removed = [
        (user_id, key)
        for user_id, keys in previous.items()
        for key in keys - current.get(user_id, frozenset())
    ]
    added = [
        (user_id, key)
        for user_id, keys in current.items()
        for key in keys - previous.get(user_id, frozenset())
    ]
    if not removed and not added:
        return
    async with session_scope() as session:
        if removed:
            await session.execute(
                delete(StaffPushAnnouncement).where(
                    tuple_(StaffPushAnnouncement.user_id, StaffPushAnnouncement.call_key).in_(
                        removed
                    )
                )
            )
        if added:
            now = utc_now()
            await session.execute(
                insert(StaffPushAnnouncement)
                .values(
                    [
                        {
                            "tenant_id": current_tenant_id(),
                            "user_id": user_id,
                            "call_key": key,
                            "created_at": now,
                        }
                        for user_id, key in added
                    ]
                )
                .on_conflict_do_nothing()
            )


async def mark_primed(subscription_ids: list[uuid.UUID]) -> None:
    """Подписки прошли первое включение — дальше рассылка шлёт им всё новое."""
    if not subscription_ids:
        return
    async with session_scope() as session:
        await session.execute(
            update(StaffPushSubscription)
            .where(
                StaffPushSubscription.id.in_(subscription_ids),
                StaffPushSubscription.primed_at.is_(None),
            )
            .values(primed_at=utc_now())
        )
