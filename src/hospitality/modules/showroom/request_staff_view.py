"""Запросы витрины для кабинета и их срок (spec 0069 §2.4, §3; PR (в), #805).

Очередь и сигнал кабинета уже прочитали заявки — здесь к ним добавляются запросы
с контактами, одним запросом, без перечитывания заявок (канон
`table_booking.bookings_for_requests`). Строка без заявки не видна.

Срок (§3): контакты, компания и пожелания обезличиваются через 365 дней от
`created_at` — своя ветка воркера ретеншна (spec 0032), а не 90 дней гостя: сделка
по группе живёт месяцами. Брошенная строка — заявка так и не создана — удаляется
через сутки, как бронь столика без заявки.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any, Final, NamedTuple, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult

from hospitality.modules.showroom.request_models import ShowroomRequest
from hospitality.modules.showroom.request_schemas import (
    CONTACTS_RETENTION_DAYS,
    StaffShowroomRequestRead,
)
from hospitality.platform.config import LocalizedText
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Строка без заявки старше суток — брошена (Р-3 spec 0038, как у брони столика).
ABANDONED_AFTER: Final = timedelta(days=1)


class ShowroomRetention(NamedTuple):
    anonymized: int
    abandoned_deleted: int


async def requests_for_requests(
    request_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, StaffShowroomRequestRead]:
    """Запросы витрины среди уже прочитанных заявок; ключ — `request_id`."""
    if not request_ids:
        return {}
    async with session_scope() as session:
        rows = (
            await session.scalars(
                select(ShowroomRequest).where(
                    ShowroomRequest.request_id.in_(list(request_ids)),
                    ShowroomRequest.request_created,
                )
            )
        ).all()
    return {row.request_id: _staff_read(row) for row in rows}


def _staff_read(row: ShowroomRequest) -> StaffShowroomRequestRead:
    return StaffShowroomRequestRead(
        request_id=row.request_id,
        kind=row.kind,
        company=row.company,
        contact_name=row.contact_name,
        phone=row.phone,
        email=row.email,
        guests_count=row.guests_count,
        check_in=row.check_in,
        check_out=row.check_out,
        room_numbers=list(row.room_numbers),
        wish_view_names=_localized(row.wish_view_names),
        wish_type_names=_localized(row.wish_type_names),
        wishes=row.wishes,
        language=row.language,
        anonymized=row.anonymized_at is not None,
    )


def _localized(names: dict[str, str] | None) -> LocalizedText | None:
    return LocalizedText.model_validate(names) if names else None


async def anonymize_expired(now: datetime) -> ShowroomRetention:
    """Ветка ретеншна (§3): контакты старше 365 дней — обнулить; брошенные — удалить.

    Идемпотентна (P-8): обезличенные отфильтрованы `anonymized_at IS NULL`.
    Зовётся воркером ретеншна внутри `tenant_context` каждого тенанта.
    """
    cutoff = now - timedelta(days=CONTACTS_RETENTION_DAYS)
    async with session_scope() as session:
        anonymized = cast(
            "CursorResult[Any]",
            await session.execute(
                update(ShowroomRequest)
                .where(ShowroomRequest.created_at < cutoff, ShowroomRequest.anonymized_at.is_(None))
                .values(
                    company=None,
                    contact_name=None,
                    phone=None,
                    email=None,
                    wishes=None,
                    anonymized_at=now,
                )
            ),
        ).rowcount
        abandoned = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(ShowroomRequest).where(
                    ShowroomRequest.created_at < now - ABANDONED_AFTER,
                    ShowroomRequest.request_created.is_(False),
                )
            ),
        ).rowcount
    if anonymized or abandoned:
        logger.info(
            "showroom_requests_retention",
            anonymized=anonymized or 0,
            abandoned_deleted=abandoned or 0,
        )
    return ShowroomRetention(anonymized or 0, abandoned or 0)
