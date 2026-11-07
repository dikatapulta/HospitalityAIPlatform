"""Комната идентичности гостя — для раздела «Диалоги» кабинета (spec 0067 §2.1, issue #767).

Веб-диалог рождается на идентичность привязки (spec 0027 §3.2), а комнату знает
проживание: идентичность → её сессии → Stay. Каждая привязка рождает свою
идентичность, повторный вход по листку того же Stay — прежнюю (#223), поэтому у
идентичности одно проживание; на всякий случай берётся проживание последней сессии.
Номер — текущий номер проживания: переселённый гость показывается в новом.

Выехавшие проживания тоже в счёт: переписка живёт дольше проживания (spec 0032).
Обе функции читают под RLS тенанта запроса (свой `session_scope()`).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import select

from hospitality.modules.guests.models import GuestSession, Stay
from hospitality.shared.db import session_scope


async def rooms_for_identities(identity_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Номер комнаты по идентичности; идентичности без сессии в ответе нет."""
    if not identity_ids:
        return {}
    async with session_scope() as session:
        rows = await session.execute(
            select(GuestSession.guest_identity_id, Stay.room_number)
            .join(Stay, Stay.id == GuestSession.stay_id)
            .where(GuestSession.guest_identity_id.in_(list(identity_ids)))
            .order_by(GuestSession.created_at, GuestSession.id)
        )
        # Порядок по времени сессии: у повторов побеждает последняя.
        return {identity_id: room_number for identity_id, room_number in rows}


async def identities_in_room(room_number: str) -> list[uuid.UUID]:
    """Идентичности всех привязок к проживаниям этого номера — фильтр «Комната»."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(GuestSession.guest_identity_id)
            .join(Stay, Stay.id == GuestSession.stay_id)
            .where(Stay.room_number == room_number)
            .distinct()
        )
        return list(rows)
