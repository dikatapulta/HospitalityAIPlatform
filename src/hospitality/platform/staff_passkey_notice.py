"""Уведомление владельцу о привязке устройства подписи (ADR-008 §8, spec 0077 §9).

Корень доверия ключа — пароль: знающий его может и войти, и привязать своё
устройство. От этого защищает уведомление владельцу, двумя путями:

- пуш на все подписки учётки во всех отелях её действующих членств — сразу.
  Против знающего пароль это главный путь;
- плашка в кабинете при следующем входе с «Это не я» — пока её не закрыли.
  Плашку не видит и не закрывает только сессия, которая ключ привязала: знающий
  пароль войдёт второй раз и закроет её сам (ревью PR #943).

«Это не я» — с паролем, как любая отвязка (аудит #949, В2-4), и только пока
плашка открыта и не из привязавшей сессии: иначе владелец отрёкся бы от
устройства, которым уже подписывал, а подписи этим ключом теперь видны в листе
как «устройство названо чужим» (`staff_confirm.ProofStatus.DISOWNED`). Гасит ключ
и все сессии учётки, кроме текущей. Знающего пароль это не останавливает —
поэтому текст ведёт за новым паролем, а новый пароль гасит и ключи (В2-5).
Пуш — по канону `staff_push.deliver`; сбой службы пуша привязку не отменяет:
ключ уже записан, и плашка остаётся вторым путём.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from hospitality.platform.config import load_tenant_config
from hospitality.platform.models import MembershipStatus, StaffSession, TenantMembership
from hospitality.platform.staff_passkeys import (
    PasskeyInfo,
    PasskeyRevokeReason,
    StaffPasskey,
    passkey_info,
    require_password,
)
from hospitality.platform.staff_push import PushMessage, StaffPushSubscription, deliver
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ports.web_push import PushOutcome
from hospitality.shared.tenancy import tenant_context

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)

BOUND_PUSH_TITLE: Final = "Привязано устройство для подписи"
# Сутки: телефон, выключенный на ночь, получит уведомление утром.
BOUND_PUSH_TTL_SECONDS: Final = 86_400
_UTC: Final = ZoneInfo("UTC")


def binding_notice_text(device_label: str, bound_at: str) -> str:
    """Текст пуша и плашки — spec 0077 §9."""
    return (
        f"К вашей учётной записи привязано устройство для подписи: «{device_label}», "
        f"{bound_at}. Это не вы? Нажмите „Это не я“ и попросите у менеджера новый пароль."
    )


def format_bound_at(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M")


async def announce_binding(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    device_label: str,
    bound_at: datetime,
    *,
    url: str,
) -> int:
    """Пуш о привязке на все подписки учётки; число доставленных.

    Время — по поясу отеля, где привязали. Сбой не поднимается выше: привязка
    уже записана, плашка покажет то же при следующем входе."""
    try:
        return await _announce(user_id, tenant_id, device_label, bound_at, url=url)
    except Exception:
        # WARNING, а не ERROR: ERROR унёс бы в Sentry тело запроса (#433).
        logger.warning("staff.passkey_notice_failed", user_id=str(user_id), exc_info=True)
        return 0


async def _announce(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    device_label: str,
    bound_at: datetime,
    *,
    url: str,
) -> int:
    async with platform_session_scope() as session:
        try:
            tz = (await load_tenant_config(session, tenant_id)).tzinfo
        except AppError:
            tz = _UTC
        member_tenants = list(
            await session.scalars(
                select(TenantMembership.tenant_id).where(
                    TenantMembership.user_id == user_id,
                    TenantMembership.status == MembershipStatus.ACTIVE,
                )
            )
        )
    message = PushMessage(
        title=BOUND_PUSH_TITLE,
        body=binding_notice_text(device_label, format_bound_at(bound_at, tz)),
        url=url,
        tag="passkey-bound",
    )
    sent = delivered = 0
    for member_tenant in member_tenants:
        with tenant_context(member_tenant):
            async with session_scope() as session:
                subscriptions = list(
                    await session.scalars(
                        select(StaffPushSubscription).where(
                            StaffPushSubscription.user_id == user_id
                        )
                    )
                )
            for subscription in subscriptions:
                delivery = await deliver(subscription, message, ttl_seconds=BOUND_PUSH_TTL_SECONDS)
                sent += 1
                delivered += delivery.outcome is PushOutcome.DELIVERED
    logger.info(
        "staff.passkey_notice_sent", user_id=str(user_id), subscriptions=sent, delivered=delivered
    )
    return delivered


async def unseen_bindings(staff: StaffContext) -> list[PasskeyInfo]:
    """Плашка о привязке: действующие ключи, о которых владелец ещё не прочёл.

    Сессия, которая привязала ключ, плашки не видит и закрыть её не может: иначе
    знающий пароль привязал бы свой телефон и сам же убрал бы предупреждение."""
    async with platform_session_scope() as session:
        rows = await session.scalars(
            select(StaffPasskey)
            .where(
                StaffPasskey.user_id == staff.user_id,
                StaffPasskey.revoked_at.is_(None),
                StaffPasskey.owner_seen_at.is_(None),
                StaffPasskey.bound_session_id.is_distinct_from(staff.session_id),
            )
            .order_by(StaffPasskey.created_at)
        )
        return [passkey_info(key) for key in rows]


async def acknowledge_binding(staff: StaffContext, passkey_id: uuid.UUID) -> bool:
    """«Это я» — плашка о привязке закрыта; False — нечего закрывать из этой сессии."""
    async with platform_session_scope() as session:
        seen = await session.scalars(
            update(StaffPasskey)
            .where(
                StaffPasskey.id == passkey_id,
                StaffPasskey.user_id == staff.user_id,
                StaffPasskey.owner_seen_at.is_(None),
                StaffPasskey.bound_session_id.is_distinct_from(staff.session_id),
            )
            .values(owner_seen_at=utc_now())
            .returning(StaffPasskey.id)
        )
        return bool(list(seen))


async def disown_binding(
    staff: StaffContext,
    passkey_id: uuid.UUID,
    password: str,
    *,
    client_ip: str,
    device_token: str | None,
) -> bool:
    """«Это не я»: ключ погашен, все сессии учётки, кроме этой, — тоже.

    Пароль — под бюджетами входа (ERR-AUTH-017, ERR-AUTH-006). False — нечего
    гасить из этой сессии: ключа нет, он погашен, плашку уже закрыли «Это я» или
    сессия сама его привязала. Пароль чужой уже знает: дальше — новый пароль
    (текст плашки, spec 0077 §9), погашенные сессии лишь выгоняют вошедших."""
    await require_password(staff, password, client_ip=client_ip, device_token=device_token)
    now = utc_now()
    async with platform_session_scope() as session:
        disowned = await session.scalar(
            update(StaffPasskey)
            .where(
                StaffPasskey.id == passkey_id,
                StaffPasskey.user_id == staff.user_id,
                StaffPasskey.revoked_at.is_(None),
                StaffPasskey.owner_seen_at.is_(None),
                StaffPasskey.bound_session_id.is_distinct_from(staff.session_id),
            )
            .values(
                revoked_at=now,
                revoked_reason=PasskeyRevokeReason.DISOWNED.value,
                owner_seen_at=now,
            )
            .returning(StaffPasskey.id)
        )
        if disowned is None:
            return False
        sessions = await session.scalars(
            update(StaffSession)
            .where(
                StaffSession.user_id == staff.user_id,
                StaffSession.id != staff.session_id,
                StaffSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
            .returning(StaffSession.id)
        )
        revoked_sessions = len(list(sessions))
    logger.warning(
        "staff.passkey_disowned",
        user_id=str(staff.user_id),
        passkey_id=str(passkey_id),
        revoked_sessions=revoked_sessions,
    )
    return True
