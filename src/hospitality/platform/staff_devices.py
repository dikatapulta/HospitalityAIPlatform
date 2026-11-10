"""Знакомое устройство входа кабинета (spec 0033 §3.3, issue #402).

Код отеля публичен (гостевой QR), логин угадывается по бейджу, а за IP — весь
отель вместе с гостевым Wi-Fi: ключи бюджета входа (`staff_credentials`) гость
исчерпывал сам и запирал вход сотруднику или всему отелю. Браузер, где по
логину уже входили с верным паролем, держит cookie со случайным токеном
(ставит `staff_portal/browser.py`); таблица `staff_known_devices` помнит пары
«SHA-256 токена + учётка». Вход со знакомого для логина устройства идёт мимо
ключей учётки и IP — со своим ключом на пару «устройство + учётка»
(`staff_credentials._login_budgets`).

Здесь: таблица, поиск знакомого устройства по логину (`known_device_key`) и
запись пары успешным входом (`remember_device`). Токен — секрет доступа к
лимиту (PII_REGISTRY, «Не-PII, но секреты»): в БД только хэш, в логи не пишется.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import ForeignKey, Index, String, Uuid, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.platform.models import UserIdentity, UserIdentityKind
from hospitality.platform.staff_credentials import password_external_id
from hospitality.shared.db import Base, UTCDateTime, platform_session_scope, utc_now

# Сколько дней браузер остаётся знакомым логину от последнего входа по нему;
# столько же живёт cookie (spec 0033 §3.3).
KNOWN_DEVICE_TTL_DAYS: Final = 365

# Токен — `secrets.token_urlsafe(32)`: 43 знака base64url. Значение cookie
# другого вида (вписанное руками, обрезанное) не ищется, а вход выдаёт новое.
_TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{43}")


class StaffKnownDevice(Base):
    """Браузер, где учётка уже входила с верным паролем (spec 0033 §3.3).

    Платформенный мир, вне RLS — как `staff_sessions` (ADR-008 §6): учётка
    принадлежит человеку, а не тенанту. Одна строка — пара «токен браузера +
    учётка»: на общем компьютере стойки у одного токена строк столько, сколько
    логинов на нём входило.
    """

    __tablename__ = "staff_known_devices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 hex токена из cookie (канон `staff_sessions.token_hash`).
    token_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


Index(
    "uq_staff_known_devices_token_hash_user_id",
    StaffKnownDevice.token_hash,
    StaffKnownDevice.user_id,
    unique=True,
)


def _valid_token(device_token: str | None) -> str | None:
    if device_token is None or not _TOKEN_PATTERN.fullmatch(device_token):
        return None
    return device_token


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def known_device_key(
    device_token: str | None, tenant_id: uuid.UUID, staff_login: str
) -> str | None:
    """Ключ бюджета `staff_login_device`, если браузер знаком этому логину отеля.

    Знаком — есть строка пары «токен + учётка с этим логином», и вход по ней был
    не раньше `KNOWN_DEVICE_TTL_DAYS` назад. Иначе None — вход идёт обычными
    ключами. Ключ — id строки: он и есть пара «устройство + учётка», не секрет
    и не PII. Оракула нет: чтобы получить ответ «знаком», нужен токен, который
    выдаётся только после верного пароля к этому логину."""
    token = _valid_token(device_token)
    if token is None:
        return None
    since = utc_now() - timedelta(days=KNOWN_DEVICE_TTL_DAYS)
    async with platform_session_scope() as session:
        row_id = await session.scalar(
            select(StaffKnownDevice.id)
            .join(UserIdentity, UserIdentity.user_id == StaffKnownDevice.user_id)
            .where(
                StaffKnownDevice.token_hash == _token_hash(token),
                StaffKnownDevice.last_used_at > since,
                UserIdentity.kind == UserIdentityKind.PASSWORD,
                UserIdentity.external_id == password_external_id(tenant_id, staff_login),
            )
        )
    return str(row_id) if row_id is not None else None


async def _issued(session: AsyncSession, token: str) -> bool:
    row_id = await session.scalar(
        select(StaffKnownDevice.id)
        .where(StaffKnownDevice.token_hash == _token_hash(token))
        .limit(1)
    )
    return row_id is not None


async def remember_device(
    session: AsyncSession, device_token: str | None, user_id: uuid.UUID
) -> str:
    """Записать браузер знакомым учётке после верного пароля; вернуть токен для cookie.

    Токен браузера переиспользуется — один на браузер, чтобы общий компьютер
    стойки помнил всех, кто на нём входил. Переиспользуется только токен,
    который сервер уже выдавал (есть хоть одна его пара): подброшенный в
    браузер чужой токен входом не становится знакомым, вместо него — новый.
    Повторный вход продлевает срок пары. Зовётся в транзакции входа, рядом с
    созданием сессии."""
    token = _valid_token(device_token)
    if token is None or not await _issued(session, token):
        token = secrets.token_urlsafe(32)
    now = utc_now()
    await session.execute(
        insert(StaffKnownDevice)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=_token_hash(token),
            created_at=now,
            last_used_at=now,
        )
        .on_conflict_do_update(index_elements=["token_hash", "user_id"], set_={"last_used_at": now})
    )
    return token
