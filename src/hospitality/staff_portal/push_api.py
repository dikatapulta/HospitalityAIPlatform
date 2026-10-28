"""JSON-действия веб-пуша кабинета (spec 0050 §4–§5, issue #477).

Записать подписку телефона, удалить её («Выйти»), прислать проверочное
уведомление. Зовёт их `static/push.js`. Канон — `turnover_api.py`: CSRF-щит
`browser.require_json_same_origin`, личность — `staff_auth.require_role`,
исходы — конверт ошибок R-8. Уровень — любой: телефон звать нужно каждому.
Пуш выключен на контуре (`STAFF_PUSH_VAPID_PRIVATE_KEY` пуст) — 404 до CSRF и
личности, как выключенная доска выездов.
"""

from __future__ import annotations

from typing import Final, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from hospitality.platform import staff_auth, staff_push
from hospitality.platform.models import StaffRole
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ports.web_push import MAX_ENDPOINT_LENGTH, web_push_or_none
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): проверки чаще раза в 10 с.
ERR_PUSH_TEST_RATE_LIMITED: Final = "ERR-PUSH-004"
_TEST_WINDOW_SECONDS: Final = 10

router = APIRouter(prefix="/{tenant_slug}/api/push", tags=["staff-portal"])

_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)


def push_context(tenant_slug: str) -> dict[str, str] | None:
    """Ключ и адреса для `push.js` в каркасе страницы отеля; `None` — пуша нет."""
    web_push = web_push_or_none()
    if web_push is None:
        return None
    base = f"/staff/{tenant_slug}/api/push"
    return {
        "key": web_push.application_server_key,
        "subscribe_url": f"{base}/subscriptions",
        "delete_url": f"{base}/subscriptions/delete",
        "test_url": f"{base}/test",
    }


class SubscriptionKeys(BaseModel):
    p256dh: str = Field(max_length=100)
    auth: str = Field(max_length=50)


class SubscriptionBody(BaseModel):
    """`PushSubscription.toJSON()` браузера: адрес службы пуша и ключи."""

    endpoint: str = Field(max_length=MAX_ENDPOINT_LENGTH)
    keys: SubscriptionKeys


class EndpointBody(BaseModel):
    endpoint: str = Field(max_length=MAX_ENDPOINT_LENGTH)


class TestView(BaseModel):
    """Исход проверки; `missing` — за сотрудником нет подписки этого телефона."""

    outcome: Literal["delivered", "gone", "rejected", "failed", "missing"]


async def _actor(request: Request) -> staff_auth.StaffContext:
    if web_push_or_none() is None:
        raise HTTPException(status_code=404)
    require_json_same_origin(request)
    return await _any_role(request)


@router.post("/subscriptions", status_code=204, summary="Записать подписку телефона")
async def subscribe(request: Request, body: SubscriptionBody) -> Response:
    """Upsert подписки за сотрудником; браузер вне белого списка — 422 ERR-PUSH-001."""
    staff = await _actor(request)
    await staff_push.save_subscription(
        staff.user_id, body.endpoint, body.keys.p256dh, body.keys.auth
    )
    return Response(status_code=204)


@router.post("/subscriptions/delete", status_code=204, summary="Удалить подписку телефона")
async def unsubscribe(request: Request, body: EndpointBody) -> Response:
    """«Выйти» на телефоне: подписка этого телефона у этого сотрудника удаляется."""
    staff = await _actor(request)
    await staff_push.delete_subscription(staff.user_id, body.endpoint)
    return Response(status_code=204)


@router.post("/test", summary="Проверочное уведомление на этот телефон")
async def send_test(request: Request, body: EndpointBody) -> TestView:
    """Одно уведомление на телефон; чаще раза в 10 с — 429 ERR-PUSH-004."""
    staff = await _actor(request)
    decision = await consume_rate_limit(
        "staff_push_test",
        f"{staff.tenant_id}:{staff.user_id}",
        limit=1,
        window_seconds=_TEST_WINDOW_SECONDS,
    )
    if decision.available and not decision.allowed:
        logger.warning("staff.push_test_rate_limited", user_id=str(staff.user_id))
        raise AppError(
            code=ERR_PUSH_TEST_RATE_LIMITED,
            message="too many test notifications, try again in a few seconds",
            status_code=429,
        )
    outcome = await staff_push.send_test(
        staff.user_id, body.endpoint, url=f"/staff/{staff.tenant_slug}"
    )
    return TestView(outcome="missing" if outcome is None else outcome.value)
