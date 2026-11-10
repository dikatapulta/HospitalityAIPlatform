"""Ключ подписи сотрудника — passkey (WebAuthn) на его устройстве (ADR-008 §8, spec 0077 §4).

Фактор подтверждения действия, не способ входа: сессию ключ не открывает и в
`user_identities` не живёт. Здесь — таблица `staff_passkeys`, список и отвязка
ключей, повторный ввод пароля и общие для церемоний кирпичи: идентификатор сайта,
одноразовое состояние в Redis, код отказа подтверждения. Привязка устройства —
`staff_passkey_binding.py`, уведомление и плашка о привязке —
`staff_passkey_notice.py`, подтверждение действия — `staff_confirm.py` (R-3).

Ключ гасится пометкой `revoked_at`, а не удаляется: его открытым ключом
проверяются подписи прошлых лет (`staff_confirm.verify_proof`, ревью PR #938).

Идентификатор сайта (RP ID) — хост `PUBLIC_BASE_URL`, источник — его схема и хост:
ключ привязан к адресу кабинета (ADR-008 §8, «Последствия»), отдельной настройки нет.
Состояние церемонии — одноразовая запись в Redis на 5 минут (spec 0077 §4);
Redis недоступен — церемония не начинается (fail-closed), в отличие от лимитов.

В логах — id ключа и учётки; название устройства и id ключа WebAuthn не пишутся.
"""

from __future__ import annotations

import enum
import json
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import redis.asyncio as redis
from pydantic import BaseModel
from sqlalchemy import BigInteger, ForeignKey, LargeBinary, String, Uuid, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.platform.models import UserIdentity, UserIdentityKind
from hospitality.platform.staff_credentials import (
    enforce_login_rate_limit,
    password_external_id,
    record_failed_login,
    verify_password,
)
from hospitality.platform.staff_devices import known_device_key
from hospitality.shared import ratelimit
from hospitality.shared.config import get_settings
from hospitality.shared.db import Base, UTCDateTime, platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_CONFIRMATION_FAILED: Final = "ERR-AUTH-017"

# Телефон, планшет, второй телефон — пяти хватает (spec 0077 §4).
MAX_PASSKEYS_PER_USER: Final = 5
CEREMONY_TTL_SECONDS: Final = 300
DEVICE_LABEL_MAX_LENGTH: Final = 60

_STORE_ERRORS: Final = (OSError, redis.RedisError, TimeoutError)


class PasskeyRevokeReason(enum.StrEnum):
    """Почему ключ погашен — видно в строке, для разбора спора."""

    SELF = "self"  # отвязал сам сотрудник, с паролем
    DISOWNED = "disowned"  # «Это не я» на плашке о привязке
    MANAGER = "manager"  # «Сбросить подпись» менеджером
    # Новый пароль по ссылке: ключ мог привязать знавший старый (аудит #949, В2-5).
    PASSWORD_RESET = "password_reset"
    DEACTIVATED = "deactivated"
    ERASED = "erased"


class StaffPasskey(Base):
    """Ключ WebAuthn на устройстве сотрудника (ADR-008 §8).

    Платформенный мир, вне RLS — как `staff_sessions` (ADR-008 §6): ключ
    принадлежит User, а не членству. Закрытый ключ не покидает устройство; здесь —
    открытый (COSE) и счётчик подписей. `bound_session_id` — сессия, которая
    привязала: плашку о привязке показывают и закрывают другие сессии владельца.
    `device_label` — из закрытого словаря по User-Agent (`staff_passkey_binding`).
    """

    __tablename__ = "staff_passkeys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[bytes] = mapped_column(LargeBinary(), unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary())
    # Счётчик WebAuthn — uint32: в INTEGER со знаком не помещается.
    sign_count: Mapped[int] = mapped_column(BigInteger(), default=0)
    device_label: Mapped[str] = mapped_column(String(DEVICE_LABEL_MAX_LENGTH))
    bound_session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    owner_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revoked_reason: Mapped[str | None] = mapped_column(String(16))


class PasskeyInfo(BaseModel):
    """Ключ для экрана «Моя подпись» и плашки о привязке."""

    id: uuid.UUID
    device_label: str
    created_at: datetime
    last_used_at: datetime | None


@dataclass(frozen=True)
class RelyingParty:
    """Идентификатор сайта и источник WebAuthn — из `PUBLIC_BASE_URL`."""

    rp_id: str
    origin: str


def relying_party() -> RelyingParty:
    parts = urlsplit(get_settings().public_base_url)
    return RelyingParty(
        rp_id=parts.hostname or "localhost", origin=f"{parts.scheme}://{parts.netloc}"
    )


def confirmation_refused(reason: str, *, status_code: int = 403) -> AppError:
    """ERR-AUTH-017: подтверждение (ключ или повторный пароль) не прошло."""
    return AppError(
        code=ERR_AUTH_CONFIRMATION_FAILED,
        message=f"Confirmation failed: {reason}",
        status_code=status_code,
        details=[{"reason": reason}],
    )


def _now() -> float:
    return time.time()


async def put_ceremony(key: str, state: dict[str, str]) -> bool:
    """Записать состояние церемонии на `CEREMONY_TTL_SECONDS`; False — Redis недоступен."""
    payload = json.dumps({**state, "expires_at": str(_now() + CEREMONY_TTL_SECONDS)})
    client = ratelimit.create_redis_client()
    try:
        await client.set(key, payload, px=CEREMONY_TTL_SECONDS * 1000)
    except _STORE_ERRORS:
        logger.warning("staff.ceremony_store_unavailable", exc_info=True)
        return False
    finally:
        with suppress(Exception):
            await client.aclose()
    return True


async def take_ceremony(key: str) -> dict[str, str] | None:
    """Забрать состояние одним GETDEL: второй ответ на ту же церемонию его не найдёт."""
    client = ratelimit.create_redis_client()
    try:
        raw = await client.getdel(key)
    except _STORE_ERRORS:
        logger.warning("staff.ceremony_store_unavailable", exc_info=True)
        return None
    finally:
        with suppress(Exception):
            await client.aclose()
    if raw is None:
        return None
    state: dict[str, str] = json.loads(raw)
    # Срок — и в самой записи: TTL Redis не единственная граница.
    if float(state.pop("expires_at", "0")) <= _now():
        return None
    return state


async def require_password(
    staff: StaffContext, password: str, *, client_ip: str, device_token: str | None
) -> None:
    """Повторный ввод пароля кабинета — под бюджетами входа (ADR-008 §8).

    Вторая дверь, где доказывается пароль, рядом со входом: ключи бюджета те же
    (`staff_credentials`), отказ тратит попытку, исчерпанный бюджет — ERR-AUTH-006.
    Не подошёл — ERR-AUTH-017 с причиной `password`."""
    login = staff.login
    if login is None:
        raise confirmation_refused("password")
    device_key = await known_device_key(device_token, staff.tenant_id, login)
    await enforce_login_rate_limit(staff.tenant_id, login, client_ip, device_key=device_key)
    async with platform_session_scope() as session:
        secret_hash = await session.scalar(
            select(UserIdentity.secret_hash).where(
                UserIdentity.user_id == staff.user_id,
                UserIdentity.kind == UserIdentityKind.PASSWORD,
                UserIdentity.external_id == password_external_id(staff.tenant_id, login),
            )
        )
    if secret_hash is not None and await verify_password(password, secret_hash):
        return
    await record_failed_login(
        staff.tenant_id, login, client_ip, known_login=True, device_key=device_key
    )
    logger.warning("staff.password_recheck_failed", user_id=str(staff.user_id))
    raise confirmation_refused("password")


async def active_passkeys(session: AsyncSession, user_id: uuid.UUID) -> list[StaffPasskey]:
    rows = await session.scalars(
        select(StaffPasskey)
        .where(StaffPasskey.user_id == user_id, StaffPasskey.revoked_at.is_(None))
        .order_by(StaffPasskey.created_at)
    )
    return list(rows)


def passkey_info(passkey: StaffPasskey) -> PasskeyInfo:
    return PasskeyInfo(
        id=passkey.id,
        device_label=passkey.device_label,
        created_at=passkey.created_at,
        last_used_at=passkey.last_used_at,
    )


async def list_passkeys(user_id: uuid.UUID) -> list[PasskeyInfo]:
    """Действующие ключи сотрудника — экран «Моя подпись»."""
    async with platform_session_scope() as session:
        return [passkey_info(key) for key in await active_passkeys(session, user_id)]


async def revoke_own_passkey(
    staff: StaffContext,
    passkey_id: uuid.UUID,
    password: str,
    *,
    client_ip: str,
    device_token: str | None,
) -> bool:
    """Отвязать своё устройство с паролем; False — такого действующего ключа нет."""
    await require_password(staff, password, client_ip=client_ip, device_token=device_token)
    async with platform_session_scope() as session:
        revoked = await _revoke(
            session, staff.user_id, PasskeyRevokeReason.SELF, passkey_id=passkey_id
        )
    logger.info("staff.passkey_revoked", user_id=str(staff.user_id), reason="self", count=revoked)
    return revoked > 0


async def revoke_user_passkeys(
    session: AsyncSession, user_id: uuid.UUID, reason: PasskeyRevokeReason
) -> int:
    """Погасить все ключи учётки в транзакции вызывающего (деактивация, стирание, сброс)."""
    return await _revoke(session, user_id, reason)


async def _revoke(
    session: AsyncSession,
    user_id: uuid.UUID,
    reason: PasskeyRevokeReason,
    *,
    passkey_id: uuid.UUID | None = None,
) -> int:
    statement = update(StaffPasskey).where(
        StaffPasskey.user_id == user_id, StaffPasskey.revoked_at.is_(None)
    )
    if passkey_id is not None:
        statement = statement.where(StaffPasskey.id == passkey_id)
    now = utc_now()
    revoked = await session.scalars(
        statement.values(revoked_at=now, revoked_reason=reason.value).returning(StaffPasskey.id)
    )
    return len(list(revoked))
