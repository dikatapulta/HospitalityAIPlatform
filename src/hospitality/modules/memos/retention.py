"""Срок хранения записей (spec 0077 §8) — шаг прохода `channels/common/retention.py`.

Черновик и отозванный неотправленный — по своему `updated_at` и границе текстов
(`MESSAGES_RETENTION_DAYS`, 90 дней). Отправленная запись — `memo_retention_days`
отеля от `sent_at`; каскадом уходят лист подписей и просмотры. Требование — тем же
сроком от создания, но не раньше последней объяснительной по нему: иначе поданная
потеряла бы «по требованию» и срок. Фото требования удаляется до строки (канон
`files.delete_expired_files`: сначала файл, потом строка) — не удалилось, строка
доживёт до следующего прохода. Погашенный образец подписи, на который больше не
ссылается ни один лист, удаляется тем же проходом. Конфиг отеля не читается (не
задан, дрейф) — удаляются только черновики: срок отправленных неизвестен, и короче
него не стираем.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, delete, exists, select
from sqlalchemy.engine import CursorResult

from hospitality.modules.memos.demands import delete_photo_quietly
from hospitality.modules.memos.models import Memo, MemoDemand, MemoSignature, MemoSignatureSample
from hospitality.modules.memos.service import hotel_config
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)


def _unreferenced_demand() -> ColumnElement[bool]:
    return ~exists().where(Memo.demand_id == MemoDemand.id)


async def delete_expired_memos(*, drafts_before: datetime) -> int:
    """Удалить истёкшие записи текущего отеля; вернуть число удалённых записей."""
    try:
        retention_days: int | None = (await hotel_config(current_tenant_id())).memo_retention_days
    except AppError as exc:
        logger.warning("memo_retention_config_unavailable", error_code=exc.code)
        retention_days = None
    expired_demands: list[tuple[uuid.UUID, uuid.UUID]] = []
    async with session_scope() as session:
        deleted = (
            cast(
                "CursorResult[Any]",
                await session.execute(
                    delete(Memo).where(Memo.sent_at.is_(None), Memo.updated_at < drafts_before)
                ),
            ).rowcount
            or 0
        )
        if retention_days is not None:
            sent_before = utc_now() - timedelta(days=retention_days)
            deleted += (
                cast(
                    "CursorResult[Any]",
                    await session.execute(delete(Memo).where(Memo.sent_at < sent_before)),
                ).rowcount
                or 0
            )
            rows = await session.execute(
                select(MemoDemand.id, MemoDemand.file_id).where(
                    MemoDemand.created_at < sent_before, _unreferenced_demand()
                )
            )
            expired_demands = [(demand_id, file_id) for demand_id, file_id in rows]
        await session.execute(
            delete(MemoSignatureSample).where(
                MemoSignatureSample.revoked_at.is_not(None),
                ~exists().where(MemoSignature.sample_id == MemoSignatureSample.id),
            )
        )
    for demand_id, file_id in expired_demands:
        if not await delete_photo_quietly(file_id):
            continue
        async with session_scope() as session:
            await session.execute(
                delete(MemoDemand).where(MemoDemand.id == demand_id, _unreferenced_demand())
            )
    return deleted
