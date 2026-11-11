"""Пуши участникам записки (spec 0077 §3, §9; #936, PR 4б-2) — канон `staff_push.deliver`,
как уведомление о привязке (`platform/staff_passkey_notice.py`).

Текст — без заголовка записки и без решения: пуш видно на заблокированном экране
(§9). Нажатие ведёт на карточку записи. Шаг записи уже записан, когда идёт пуш: сбой
службы пуша его не отменяет — запись видна во вкладке «Ждут меня» и числом в меню.
Поэтому сбой не поднимается выше, а пишется WARNING (ERROR унёс бы в Sentry тело
запроса, #433). Пуша на контуре нет — молча ничего. В логах — id записи и числа.
Требование объяснения (PR 5б) шлётся тем же путём: вместо id записи — id требования.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Final

from sqlalchemy import select

from hospitality.platform.staff_push import PushMessage, StaffPushSubscription, deliver
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger
from hospitality.shared.ports.web_push import PushOutcome, web_push_or_none
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import memos_texts as texts

logger = get_logger(module=__name__)

# Сутки: телефон, выключенный на ночь, получит уведомление утром.
MEMO_PUSH_TTL_SECONDS: Final = 86_400


async def notify(
    tenant_id: uuid.UUID,
    memo_id: uuid.UUID,
    user_ids: Collection[uuid.UUID],
    body: str,
    *,
    url: str,
) -> int:
    """Пуш на все телефоны этих сотрудников в этом отеле; число доставленных."""
    if web_push_or_none() is None or not user_ids:
        return 0
    try:
        return await _send(tenant_id, memo_id, user_ids, body, url=url)
    except Exception:
        logger.warning("memo_push_failed", memo_id=str(memo_id), exc_info=True)
        return 0


async def _send(
    tenant_id: uuid.UUID,
    memo_id: uuid.UUID,
    user_ids: Collection[uuid.UUID],
    body: str,
    *,
    url: str,
) -> int:
    # Одна метка на запись: следующий шаг заменяет прежнее уведомление, а не копит их.
    message = PushMessage(title=texts.PUSH_TITLE, body=body, url=url, tag=f"memo-{memo_id}")
    with tenant_context(tenant_id):
        async with session_scope() as session:
            subscriptions = list(
                await session.scalars(
                    select(StaffPushSubscription).where(
                        StaffPushSubscription.user_id.in_(set(user_ids))
                    )
                )
            )
        delivered = 0
        for subscription in subscriptions:
            delivery = await deliver(subscription, message, ttl_seconds=MEMO_PUSH_TTL_SECONDS)
            delivered += delivery.outcome is PushOutcome.DELIVERED
    logger.info(
        "memo_push_sent",
        memo_id=str(memo_id),
        recipients=len(set(user_ids)),
        subscriptions=len(subscriptions),
        delivered=delivered,
    )
    return delivered
