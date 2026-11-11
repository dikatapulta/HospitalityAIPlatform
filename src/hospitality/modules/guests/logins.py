"""Подключения гостей за отрезок времени — число «пульса пользы» (spec 0035 §8.1).

Подключение — новая гостевая сессия: код заселения или ссылка с листка, веб и Telegram
(`service._bind_identity_and_session`). Считает текущий тенант под RLS (P-4); границы
окна — от вызывающего.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from hospitality.modules.guests.models import GuestSession
from hospitality.shared.db import session_scope


async def count_guest_logins(*, created_after: datetime, created_before: datetime) -> int:
    """Новые гостевые сессии в полуоткрытом окне `[created_after, created_before)`."""
    async with session_scope() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(GuestSession)
            .where(
                GuestSession.created_at >= created_after,
                GuestSession.created_at < created_before,
            )
        )
    return int(total or 0)
