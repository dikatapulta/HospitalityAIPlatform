"""Срок хранения данных о проживании (spec 0032 §6, #948).

Политика обещает одно число: данные о проживании живут столько же, сколько
тексты гостя, — `messages_retention_days` от выезда. Исполняет обещание
`delete_expired_stay_data`: её раз в час зовёт проход ретеншна канального ядра
(`channels/common/retention.py`) внутри `tenant_context` каждого отеля.

Одна транзакция, три шага:

1. Stay со сроком выезда старше границы — удаляется при любом статусе; коды
   заселения, ссылки привязки и сессии уходят каскадом (`ON DELETE CASCADE`).
   Живое проживание не трогается: продление двигает `check_out_at`.
2. Гость без единого Stay, заведённый раньше границы, — удаляется, идентичности
   уходят каскадом. Идентичность, у которой осталась сессия на живом Stay
   ДРУГОГО гостя (чат Telegram вернувшегося гостя: повторная привязка `guest_id`
   не перевешивает, `service._bind_identity_and_session`), перед этим
   переезжает к гостю этого Stay — иначе каскад разлогинил бы живого гостя.
3. Записи о согласии старше своего срока (`consent_record_retention_days`).

Идемпотентна (P-8): повторный прогон не находит строк. Ссылки других модулей
на Stay и идентичность — без FK (граница модулей): их обнуляет собственный
ретеншн владельца по возрасту строки.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final, cast

from sqlalchemy import delete, exists, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.guests.models import (
    ConsentRecord,
    Guest,
    GuestIdentity,
    GuestSession,
    Stay,
)
from hospitality.modules.guests.schemas import StayDataRetentionStats
from hospitality.shared.db import session_scope

# Потолок гостей на отель за один прогон: список id уходит параметрами запроса.
# Остаток не теряется — его подберёт следующий прогон через час.
_GUEST_BATCH: Final = 1000


async def delete_expired_stay_data(
    *, checked_out_before: datetime, consents_before: datetime
) -> StayDataRetentionStats:
    """Один проход по текущему тенанту; вернуть счётчики и удалённые идентичности."""
    async with session_scope() as session:
        stays_deleted = _rowcount(
            await session.execute(delete(Stay).where(Stay.check_out_at < checked_out_before))
        )
        guest_ids = list(
            await session.scalars(
                select(Guest.id)
                .where(
                    Guest.created_at < checked_out_before,
                    ~exists().where(Stay.guest_id == Guest.id),
                )
                .order_by(Guest.created_at)
                .limit(_GUEST_BATCH)
            )
        )
        identity_ids: list[uuid.UUID] = []
        guests_deleted = 0
        if guest_ids:
            await _rehome_identities_in_use(session, guest_ids)
            identity_ids = list(
                await session.scalars(
                    select(GuestIdentity.id).where(GuestIdentity.guest_id.in_(guest_ids))
                )
            )
            guests_deleted = _rowcount(
                await session.execute(delete(Guest).where(Guest.id.in_(guest_ids)))
            )
        consent_records_deleted = _rowcount(
            await session.execute(
                delete(ConsentRecord).where(ConsentRecord.consent_at < consents_before)
            )
        )
    return StayDataRetentionStats(
        stays_deleted=stays_deleted,
        guests_deleted=guests_deleted,
        consent_records_deleted=consent_records_deleted,
        deleted_identity_ids=tuple(identity_ids),
    )


async def _rehome_identities_in_use(session: AsyncSession, guest_ids: Sequence[uuid.UUID]) -> None:
    """Идентичности уходящих гостей с сессией на уцелевшем Stay — к гостю этого Stay.

    Просроченные Stay уже удалены вместе с сессиями, так что любая оставшаяся
    сессия — на проживании, чей срок хранения не вышел. Сессий несколько —
    побеждает самая свежая привязка (порядок выборки).
    """
    rows = await session.execute(
        select(GuestIdentity, Stay.guest_id)
        .join(GuestSession, GuestSession.guest_identity_id == GuestIdentity.id)
        .join(Stay, Stay.id == GuestSession.stay_id)
        .where(GuestIdentity.guest_id.in_(guest_ids))
        .order_by(GuestSession.created_at)
    )
    for identity, guest_id in rows:
        identity.guest_id = guest_id
    await session.flush()


def _rowcount(result: Any) -> int:
    return cast("CursorResult[Any]", result).rowcount or 0
