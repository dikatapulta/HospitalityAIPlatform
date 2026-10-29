"""Подписки телефонов персонала на веб-пуш и отправка (spec 0050 §4–§5, issue #477).

Единственная дверь кабинета и рассылки к порту `WebPushPort` — как
`platform/files.py` к хранилищу файлов. Здесь:

- таблица подписок `staff_push_subscriptions` (тенантная, канон
  `TenantIsolationCanary`) — подписка на членстве: тенант + сотрудник;
- запись и удаление подписки, проверочное уведомление;
- `deliver` — одна отправка и её последствия: `gone` и `rejected` удаляют
  подписку, `delivered` ставит `last_delivered_at`;
- удаление подписок стёртой учётки (`delete_member_subscriptions`).

Кому и что шлёт рассылка воркера — `staff_push_journal.py` и
`staff_portal/push_sweep.py` (spec 0050 §6).

Сеть — вне транзакций БД (ADR-016): строка читается одной транзакцией, пуш
уходит без открытой транзакции, исход пишется второй. В логах — id подписки и
хост службы пуша, не адрес: адрес подписки — идентификатор телефона сотрудника
(docs/PII_REGISTRY.md). Текста гостя в сообщениях нет: уведомление видно на
заблокированном экране (spec 0050 §5).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Final
from urllib.parse import urlsplit

from pydantic import BaseModel
from sqlalchemy import ForeignKey, Index, String, Uuid, case, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_push_send
from hospitality.shared.ports.web_push import (
    InvalidPushSubscriptionError,
    PushDelivery,
    PushOutcome,
    build_push_target,
    get_web_push,
)
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_PUSH_SUBSCRIPTION_REJECTED: Final = "ERR-PUSH-001"
ERR_PUSH_REJECTED: Final = "ERR-PUSH-002"
ERR_PUSH_UNAVAILABLE: Final = "ERR-PUSH-003"

# Телефон, планшет, второй браузер — пяти хватает; шестая подписка вытесняет
# самую давнюю, чтобы брошенные телефоны не копились (spec 0050 §4).
MAX_SUBSCRIPTIONS_PER_MEMBER: Final = 5
# Проверочное уведомление: не дошло за минуту — сотрудник уже нажал ещё раз.
TEST_TTL_SECONDS: Final = 60

TEST_TITLE: Final = "Проверка уведомлений"
TEST_BODY: Final = "Всё работает — так телефон позовёт о новой заявке"


class StaffPushSubscription(Base):
    """Подписка браузера сотрудника на веб-пуш (spec 0050 §4).

    Тенантная таблица по канону `TenantIsolationCanary` (P-4). Ключи —
    base64url, как их отдаёт браузер: секрет `auth` без приватного ключа VAPID
    отправить пуш не даёт (служба пуша сверяет подпись), поэтому хранится открыто.
    """

    __tablename__ = "staff_push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Адрес службы пуша — идентификатор телефона сотрудника (PII_REGISTRY).
    endpoint: Mapped[str] = mapped_column(String(1024))
    p256dh: Mapped[str] = mapped_column(String(100))
    auth: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    last_delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Первое включение пройдено (spec 0050 §6): до него рассылка вносит
    # несрочное в журнал молча. Телефон перешёл к другому сотруднику — снова NULL.
    primed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


Index(
    "uq_staff_push_subscriptions_tenant_endpoint",
    StaffPushSubscription.tenant_id,
    StaffPushSubscription.endpoint,
    unique=True,
)
Index(
    "ix_staff_push_subscriptions_tenant_user",
    StaffPushSubscription.tenant_id,
    StaffPushSubscription.user_id,
)


class PushMessage(BaseModel):
    """Что показывает service worker: заголовок, строка, куда ведёт нажатие, метка.

    Одинаковая метка (`tag`) заменяет прежнее уведомление, а не добавляет второе.
    """

    title: str
    body: str
    url: str
    tag: str

    def to_payload(self) -> bytes:
        return json.dumps(self.model_dump(), ensure_ascii=False).encode("utf-8")


def _push_host(endpoint: str) -> str:
    try:
        return urlsplit(endpoint).hostname or ""
    except ValueError:  # отвергнутый адрес, который и не разбирается
        return ""


async def save_subscription(user_id: uuid.UUID, endpoint: str, p256dh: str, auth: str) -> None:
    """Записать подписку телефона за сотрудником (в контексте тенанта).

    Upsert по `(tenant_id, endpoint)`: тот же телефон под другим логином
    переходит к новому сотруднику. Сверх `MAX_SUBSCRIPTIONS_PER_MEMBER` самые
    давние подписки сотрудника удаляются. Негодная подписка — 422 ERR-PUSH-001.
    """
    try:
        build_push_target(endpoint, p256dh, auth)
    except InvalidPushSubscriptionError as error:
        logger.warning(
            "staff_push.subscription_rejected",
            error_code=ERR_PUSH_SUBSCRIPTION_REJECTED,
            reason=str(error),
            push_host=_push_host(endpoint)[:100],
        )
        raise AppError(
            code=ERR_PUSH_SUBSCRIPTION_REJECTED,
            message="push subscription is not supported",
            status_code=422,
        ) from None
    now = utc_now()
    async with session_scope() as session:
        statement = insert(StaffPushSubscription).values(
            id=uuid.uuid4(),
            tenant_id=current_tenant_id(),
            user_id=user_id,
            endpoint=endpoint,
            p256dh=p256dh,
            auth=auth,
            created_at=now,
            updated_at=now,
        )
        # Телефон перешёл к другому сотруднику — для него это первое включение:
        # иначе всё, что уже ждёт нового владельца, прозвучало бы разом.
        primed_at = case(
            (StaffPushSubscription.user_id == user_id, StaffPushSubscription.primed_at),
            else_=None,
        )
        upsert = statement.on_conflict_do_update(
            index_elements=[StaffPushSubscription.tenant_id, StaffPushSubscription.endpoint],
            set_={
                "user_id": user_id,
                "p256dh": p256dh,
                "auth": auth,
                "updated_at": now,
                "primed_at": primed_at,
            },
        ).returning(StaffPushSubscription.id)
        subscription_id = (await session.execute(upsert)).scalar_one()
        stale = await session.scalars(
            select(StaffPushSubscription.id)
            .where(StaffPushSubscription.user_id == user_id)
            .order_by(StaffPushSubscription.updated_at.desc(), StaffPushSubscription.id)
            .offset(MAX_SUBSCRIPTIONS_PER_MEMBER)
        )
        stale_ids = list(stale)
        if stale_ids:
            await session.execute(
                delete(StaffPushSubscription).where(StaffPushSubscription.id.in_(stale_ids))
            )
    logger.info(
        "staff_push.subscribed",
        subscription_id=str(subscription_id),
        user_id=str(user_id),
        push_host=_push_host(endpoint),
        evicted=len(stale_ids),
    )


async def delete_subscription(user_id: uuid.UUID, endpoint: str) -> bool:
    """Удалить подписку этого телефона у сотрудника («Выйти», spec 0050 §4)."""
    async with session_scope() as session:
        deleted = await session.scalars(
            delete(StaffPushSubscription)
            .where(
                StaffPushSubscription.user_id == user_id,
                StaffPushSubscription.endpoint == endpoint,
            )
            .returning(StaffPushSubscription.id)
        )
        deleted_ids = list(deleted)
    if deleted_ids:
        logger.info(
            "staff_push.unsubscribed", subscription_id=str(deleted_ids[0]), user_id=str(user_id)
        )
    return bool(deleted_ids)


async def send_test(user_id: uuid.UUID, endpoint: str, *, url: str) -> PushOutcome | None:
    """Проверочное уведомление на этот телефон; `None` — подписки за сотрудником нет."""
    async with session_scope() as session:
        subscription = await session.scalar(
            select(StaffPushSubscription).where(
                StaffPushSubscription.user_id == user_id,
                StaffPushSubscription.endpoint == endpoint,
            )
        )
    if subscription is None:
        return None
    message = PushMessage(title=TEST_TITLE, body=TEST_BODY, url=url, tag="push-test")
    delivery = await deliver(subscription, message, ttl_seconds=TEST_TTL_SECONDS)
    return delivery.outcome


async def delete_member_subscriptions(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Удалить подписки сотрудника в транзакции вызывающего («Стереть учётку»).

    Строку `users` стирание не удаляет, поэтому FK-каскад тут не помогает
    (ревью PR #501, Н-1); подписки отключённых удаляет рассылка (spec 0050 §4).
    """
    deleted = await session.scalars(
        delete(StaffPushSubscription)
        .where(StaffPushSubscription.user_id == user_id)
        .returning(StaffPushSubscription.id)
    )
    return len(list(deleted))


async def deliver(
    subscription: StaffPushSubscription,
    message: PushMessage,
    *,
    ttl_seconds: int,
    log_failure: bool = True,
) -> PushDelivery:
    """Одна отправка на одну подписку и её последствия для строки (spec 0050 §5).

    `log_failure=False` — сбой службы пуша не пишется здесь: рассылка пишет
    его одной строкой на проход (spec 0050 §9).
    """
    try:
        target = build_push_target(subscription.endpoint, subscription.p256dh, subscription.auth)
        delivery = await get_web_push().send(target, message.to_payload(), ttl_seconds=ttl_seconds)
    except InvalidPushSubscriptionError:
        # Строка, которую нынешняя проверка не пропускает (записана до её
        # ужесточения), — отказ, а не падение прохода (ревью PR #501, Н-2).
        delivery = PushDelivery(PushOutcome.REJECTED)
    outcome = delivery.outcome
    record_staff_push_send(outcome.value)
    context = {
        "subscription_id": str(subscription.id),
        "user_id": str(subscription.user_id),
        "push_host": _push_host(subscription.endpoint),
        "status_code": delivery.status_code,
        "tag": message.tag,
    }
    if outcome is PushOutcome.DELIVERED:
        await _mark_delivered(subscription.id)
        logger.info("staff_push.delivered", **context)
    elif outcome is PushOutcome.GONE:
        await _forget(subscription.id)
        logger.info("staff_push.subscription_gone", **context)
    elif outcome is PushOutcome.REJECTED:
        await _forget(subscription.id)
        # WARNING, а не ERROR, намеренно: ERROR унёс бы в Sentry тело запроса
        # (#433), а в теле проверки — адрес подписки телефона (PII_REGISTRY).
        logger.warning("staff_push.rejected", error_code=ERR_PUSH_REJECTED, **context)
    elif log_failure:
        logger.warning("staff_push.unavailable", error_code=ERR_PUSH_UNAVAILABLE, **context)
    return delivery


async def _mark_delivered(subscription_id: uuid.UUID) -> None:
    async with session_scope() as session:
        await session.execute(
            update(StaffPushSubscription)
            .where(StaffPushSubscription.id == subscription_id)
            .values(last_delivered_at=utc_now())
        )


async def _forget(subscription_id: uuid.UUID) -> None:
    async with session_scope() as session:
        await session.execute(
            delete(StaffPushSubscription).where(StaffPushSubscription.id == subscription_id)
        )
