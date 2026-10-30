"""Ссылка привязки Stay — QR на листке гостю (spec 0033 §6, ADR-008 §3).

Ресепшен выпускает ссылку с карточки заселения, печатает листок (комната, QR,
код) и отдаёт его гостю вместе с ключом; гость сканирует QR и после
consent-строки привязывается БЕЗ ввода кода. Решение основателя 21.09.2026
(#354): бумага живёт всё проживание, поэтому ссылка устроена как второе
написание кода заселения, а не как эфемерный пропуск у стойки:

- хранилище — Postgres (`StayBindLink`, RLS), в БД только SHA-256 токена;
  прежний Redis без сохранения на диск молча гасил бы напечатанные листки;
- своего срока нет: действует, пока Stay в `checked_in` и `now <
  check_out_at` — продление и выезд подхватываются автоматически;
- многоразова: семья и второе устройство входят одним листком, повторное
  сканирование того же телефона не упирается в «ссылка устарела» и возвращает
  гостя в его диалог: браузер с живой сессией того же Stay получает новую
  сессию на прежнюю идентичность (#223) — иначе итог заявки остался бы в
  диалоге, куда гость уже не попадёт;
- гаснет перевыпуском кода и выездом (`service.reissue_access_code`,
  `service.check_out`) — потерянный листок умирает целиком.

Стойкость не ниже кода с того же листка: токен 256 бит против шести цифр.
Rate-limit'ы (канон 0023) — забота вызывающих, как у ввода кода (spec 0027
§3.3): выпуск — кабинет по (tenant, stay), привязка — канал web по IP.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.guests.models import (
    GuestIdentity,
    GuestIdentityKind,
    GuestSession,
    Stay,
    StayBindLink,
    StayStatus,
)
from hospitality.modules.guests.schemas import GuestSessionBind, GuestSessionGrant
from hospitality.modules.guests.service import (
    _bind_identity_and_session,
    _get_active_stay_or_raise,
    _hash_session_token,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


def _hash_token(token: str) -> str:
    # Канон секретов ADR-008: plaintext живёт лишь в выданной ссылке (QR).
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_bind_link(stay_id: uuid.UUID) -> str:
    """Выпустить ссылку привязки активного Stay; вернуть токен.

    Токен показывается РОВНО ОДИН РАЗ (внутри QR/URL). Повторный выпуск ничего
    не гасит: уже напечатанный листок продолжает работать. Нет активного
    Stay — ERR-GUESTS-001.
    """
    token = secrets.token_urlsafe(32)
    async with session_scope() as session:
        stay = await _get_active_stay_or_raise(session, stay_id)
        session.add(StayBindLink(stay_id=stay.id, token_hash=_hash_token(token)))
    logger.info("stay_bind_link_issued", stay_id=str(stay_id), room_number=stay.room_number)
    return token


async def start_guest_session_by_bind_link(data: GuestSessionBind) -> GuestSessionGrant | None:
    """Привязка по ссылке с листка (spec 0033 §6): без проверки кода.

    Ссылка, её Stay и срок проверяются одним запросом в одной транзакции с
    рождением сессии: перевыпуск или выезд, случившиеся раньше, уже видны.
    Идентичность и сессия создаются ТЕМ ЖЕ путём, что при вводе кода (P-12:
    общий `_bind_identity_and_session`). Ссылка не найдена, погашена, чужого
    тенанта (RLS), Stay погас — `None`: исходы для гостя неразличимы.

    `current_session_token` — живая неотозванная сессия этого же Stay: новая
    сессия встаёт на её идентичность (диалог гостя прежний, #223). Сессия
    другого Stay, отозванная или чужая — не мешает: привязка как без неё.
    """
    token = secrets.token_urlsafe(32)
    async with session_scope() as session:
        stay: Stay | None = await session.scalar(
            select(Stay)
            .join(StayBindLink, StayBindLink.stay_id == Stay.id)
            .where(
                StayBindLink.token_hash == _hash_token(data.bind_token),
                StayBindLink.revoked_at.is_(None),
                Stay.status == StayStatus.CHECKED_IN,
                Stay.check_out_at > utc_now(),
            )
        )
        if stay is None:
            logger.info("stay_bind_link_rejected", reason="unknown_revoked_or_stay_gone")
            return None
        kept_external_id = (
            await _identity_of_live_session(
                session, data.current_session_token, stay=stay, kind=data.identity_kind
            )
            if data.current_session_token
            else None
        )
        identity, guest_session = await _bind_identity_and_session(
            session,
            stay,
            identity_kind=data.identity_kind,
            identity_external_id=kept_external_id or data.identity_external_id,
            consent_version=data.consent_version,
            token=token,
        )
    logger.info(
        "guest_session_started",
        stay_id=str(stay.id),
        guest_identity_id=str(identity.id),
        session_id=str(guest_session.id),
        via_bind_link=True,
        kept_identity=kept_external_id is not None,
    )
    return GuestSessionGrant(
        session_token=token,
        stay_id=stay.id,
        guest_identity_id=identity.id,
        room_number=stay.room_number,
    )


async def _identity_of_live_session(
    session: AsyncSession, token: str, *, stay: Stay, kind: GuestIdentityKind
) -> str | None:
    """`external_id` идентичности живой сессии браузера — только того же Stay
    и того же вида идентичности; Stay уже проверен запросом ссылки."""
    external_id: str | None = await session.scalar(
        select(GuestIdentity.external_id)
        .join(GuestSession, GuestSession.guest_identity_id == GuestIdentity.id)
        .where(
            GuestSession.token_hash == _hash_session_token(token),
            GuestSession.revoked_at.is_(None),
            GuestSession.stay_id == stay.id,
            GuestIdentity.kind == kind,
        )
    )
    return external_id
