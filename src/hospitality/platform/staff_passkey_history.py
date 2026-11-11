"""Что лист подписей обязан сказать о ключе, которым подтверждали (spec 0077 §4, §9; #936).

Подпись ключом сходится и тогда, когда ключ потом погашен: отвязанный телефон не
делает прошлые подписи непроверяемыми (`staff_confirm.verify_proof`). Два случая
лист называет отдельно, иначе «подтверждено с личного устройства» было бы неполной
правдой:

- ключ погашен новым паролем по ссылке (`PasskeyRevokeReason.PASSWORD_RESET`): его
  мог привязать знавший старый пароль, а плашку «Это не я» он же закрыл вторым
  входом (ревью PR #977);
- ключ привязан после сброса пароля по ссылке: задать пароль по ней мог её
  держатель и тут же привязать своё устройство (#944) — как `password_reset_at`
  у подписи паролем.

Сбросом считается ссылка нового пароля любого логина учётки: новый пароль гасит
ключи учётки во всех отелях (`staff_password_reset.accept_password_reset`).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from hospitality.platform.models import StaffPasswordReset, UserIdentity, UserIdentityKind
from hospitality.platform.staff_passkeys import PasskeyRevokeReason, StaffPasskey
from hospitality.shared.db import platform_session_scope


@dataclass(frozen=True)
class PasskeyHistory:
    # Погашен новым паролем по ссылке — когда; None — не так.
    reset_revoked_at: datetime | None
    # Последний сброс пароля по ссылке до привязки ключа; None — сброса до неё не было.
    bound_after_reset_at: datetime | None


async def passkey_history(passkey_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, PasskeyHistory]:
    """История ключей из доказательств листа; неизвестный id в ответ не попадает."""
    if not passkey_ids:
        return {}
    async with platform_session_scope() as session:
        keys = (
            await session.execute(
                select(
                    StaffPasskey.id,
                    StaffPasskey.user_id,
                    StaffPasskey.created_at,
                    StaffPasskey.revoked_at,
                    StaffPasskey.revoked_reason,
                ).where(StaffPasskey.id.in_(set(passkey_ids)))
            )
        ).all()
        users = {key.user_id for key in keys}
        resets = (
            await session.execute(
                select(UserIdentity.user_id, StaffPasswordReset.used_at)
                .join(UserIdentity, StaffPasswordReset.identity_id == UserIdentity.id)
                .where(
                    UserIdentity.user_id.in_(users),
                    UserIdentity.kind == UserIdentityKind.PASSWORD,
                    StaffPasswordReset.used_at.is_not(None),
                )
            )
        ).all()
    used: dict[uuid.UUID, list[datetime]] = {}
    for user_id, used_at in resets:
        if used_at is not None:  # отфильтровано запросом; проверка — для типов
            used.setdefault(user_id, []).append(used_at)
    history: dict[uuid.UUID, PasskeyHistory] = {}
    for key in keys:
        before = [moment for moment in used.get(key.user_id, []) if moment < key.created_at]
        by_reset = key.revoked_reason == PasskeyRevokeReason.PASSWORD_RESET
        history[key.id] = PasskeyHistory(
            reset_revoked_at=key.revoked_at if by_reset else None,
            bound_after_reset_at=max(before) if before else None,
        )
    return history
