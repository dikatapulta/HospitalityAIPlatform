"""Срок хранения записей (spec 0077 §8) — шаг прохода `channels/common/retention.py`.

Черновик и отозванный неотправленный — по своему `updated_at` и границе текстов
(`MESSAGES_RETENTION_DAYS`, 90 дней). Отправленная запись — `memo_retention_days`
отеля от `sent_at`; каскадом уходят лист подписей и просмотры. Требования — тем же
сроком от создания. Погашенный образец подписи, на который больше не ссылается ни
один лист, удаляется тем же проходом. Конфиг отеля не читается (не задан, дрейф) —
удаляются только черновики: срок отправленных неизвестен, и короче него не стираем.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, exists
from sqlalchemy.engine import CursorResult

from hospitality.modules.memos.models import Memo, MemoDemand, MemoSignature, MemoSignatureSample
from hospitality.modules.memos.service import hotel_config
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)


async def delete_expired_memos(*, drafts_before: datetime) -> int:
    """Удалить истёкшие записи текущего отеля; вернуть число удалённых записей."""
    try:
        retention_days: int | None = (await hotel_config(current_tenant_id())).memo_retention_days
    except AppError as exc:
        logger.warning("memo_retention_config_unavailable", error_code=exc.code)
        retention_days = None
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
            await session.execute(delete(MemoDemand).where(MemoDemand.created_at < sent_before))
        await session.execute(
            delete(MemoSignatureSample).where(
                MemoSignatureSample.revoked_at.is_not(None),
                ~exists().where(MemoSignature.sample_id == MemoSignatureSample.id),
            )
        )
    return deleted
