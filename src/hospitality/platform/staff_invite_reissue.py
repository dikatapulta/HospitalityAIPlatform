"""«Новая ссылка» у приглашения и истёкшие приглашения в списке (spec 0037 §4, #944).

Ссылка-приглашение показывается один раз и живёт `STAFF_INVITE_TTL_HOURS`;
потерянная или истёкшая раньше лечилась отзывом и новой формой с нуля. Здесь —
два шага без формы:

- `list_expired_invites` — истёкшие, не принятые и не отозванные приглашения
  последних `EXPIRED_INVITE_SHOWN_DAYS` дней: «Сотрудники» показывают их с
  пометкой «истекла». Позже они уходят из списка сами, раньше — «Убрать»
  (`staff_invites.revoke_invite`).
- `reissue_invite` — новая ссылка в той же строке: новый токен (в БД — хэш),
  срок заново, имя, логин, уровень и службы прежние. Прежняя ссылка гаснет
  сама — её хэша в строке больше нет.

Логин истёкшего приглашения свободен (`staff_invites._pending`) и мог уйти
другому — тогда ERR-AUTH-012 с 409, как у формы приглашения. Живое приглашение
свой логин держит само: проверять нечего. Гонку с выпуском того же логина ловит
UNIQUE идентичности при принятии, как у `create_invite`.

Тенантная граница — та же, что у отзыва: чужой, принятый, отозванный инвайт и
инвайт без логина — ERR-AUTH-004.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import ColumnElement, and_, select

from hospitality.platform import staff_invite_limit
from hospitality.platform.models import StaffInvite
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_INVALID
from hospitality.platform.staff_invites import (
    PendingInviteView,
    StaffInviteGrant,
    hash_invite_token,
    invalid_invite,
    is_login_taken,
)
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Две недели после срока: отпуск или смена графика не уносят приглашение из
# списка, а через две недели его проще выпустить заново.
EXPIRED_INVITE_SHOWN_DAYS: Final = 14


def _recently_expired(now: datetime) -> ColumnElement[bool]:
    return and_(
        StaffInvite.accepted_at.is_(None),
        StaffInvite.revoked_at.is_(None),
        StaffInvite.login.is_not(None),
        StaffInvite.expires_at <= now,
        StaffInvite.expires_at > now - timedelta(days=EXPIRED_INVITE_SHOWN_DAYS),
    )


async def list_expired_invites(tenant_id: uuid.UUID) -> list[PendingInviteView]:
    """Недавно истёкшие приглашения тенанта, свежие сверху; `expires_at` — когда истекло."""
    async with platform_session_scope() as session:
        invites = await session.scalars(
            select(StaffInvite)
            .where(StaffInvite.tenant_id == tenant_id, _recently_expired(utc_now()))
            .order_by(StaffInvite.expires_at.desc())
        )
        return [
            PendingInviteView(
                invite_id=invite.id,
                invited_name=invite.invited_name,
                login=invite.login or "",
                role_key=invite.role_key,
                service_keys=list(invite.service_keys),
                expires_at=invite.expires_at,
            )
            for invite in invites
        ]


async def reissue_invite(
    invite_id: uuid.UUID, *, tenant_id: uuid.UUID, actor_user_id: uuid.UUID
) -> StaffInviteGrant:
    """Новая ссылка взамен прежней — живой или истёкшей (spec 0037 §4).

    FOR UPDATE — сериализация с `accept_invite` и отзывом: принятие прежней
    ссылкой после смены хэша её не найдёт, принятое до — даёт ERR-AUTH-004 здесь.
    `invited_by` остаётся прежним — кто выдал новую ссылку, пишет лог. Новая
    ссылка — выпуск, поэтому под тем же лимитом, что приглашение (ERR-AUTH-019,
    `staff_invite_limit`): ключ — тот, кто её выдал."""
    await staff_invite_limit.ensure_invite_budget(tenant_id, actor_user_id)
    token = secrets.token_urlsafe(32)
    async with platform_session_scope() as session:
        invite = await session.get(StaffInvite, invite_id, with_for_update=True)
        if (
            invite is None
            or invite.tenant_id != tenant_id
            or invite.accepted_at is not None
            or invite.revoked_at is not None
            or invite.login is None
        ):
            raise invalid_invite()
        now = utc_now()
        was_expired = invite.expires_at <= now
        if was_expired and await is_login_taken(session, tenant_id, invite.login):
            raise AppError(
                code=ERR_AUTH_LOGIN_INVALID,
                message="This login is already taken in this hotel",
                status_code=409,
            )
        invite.token_hash = hash_invite_token(token)
        invite.expires_at = now + timedelta(hours=get_settings().staff_invite_ttl_hours)
        grant = StaffInviteGrant(
            invite_id=invite.id,
            invite_token=token,
            login=invite.login,
            expires_at=invite.expires_at,
        )
    await staff_invite_limit.record_invites_issued(tenant_id, actor_user_id)
    logger.info(
        "staff.invite_reissued",
        invite_id=str(invite_id),
        tenant_id=str(tenant_id),
        actor_user_id=str(actor_user_id),
        was_expired=was_expired,
    )
    return grant
