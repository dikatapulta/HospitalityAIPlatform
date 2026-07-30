"""JSON-действия кабинета персонала: взять / готово / отменить (spec 0033 §5).

Действия очереди заявок, которые зовёт ванильный JS страницы (`static/queue.js`).
Авторизация — `staff_auth.require_role` НАПРЯМУЮ (JSON-исходы: 401/403 уходят
каноническим конвертом ошибок R-8, без браузерных редиректов `_page_context`);
require_role же сверяет RLS-контекст запроса с тенантом действия (fail-closed,
ревью PR #153).

CSRF-контракт JSON-действий (докстринг `router.py`, README пакета): каждый POST
обязан нести `Content-Type: application/json` И НЕПУСТОЙ same-origin `Origin`
(fetch шлёт его всегда) — кросс-сайтовая форма не умеет JSON-тип, кросс-сайтовый
fetch не пройдёт по Origin. Нарушение — 403 `ERR-AUTH-009`. Отдельный CSRF-токен
при этом не нужен. Форма с enctype=text/plain, подделывающая JSON-тело, до
обработчика не доходит: без JSON Content-Type FastAPI не парсит тело → 422.

Сами переходы — `requests_api.change_request_status` (P-5): та же карта
`STATUS_TRANSITIONS`, те же события и уведомления гостю, что из Telegram;
конфликт (взяли раньше) — 409 `ERR-REQUESTS-003`, страница показывает
«уже взята» и обновляет список.
"""

from __future__ import annotations

import uuid
from typing import Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): JSON-действие кабинета
# отклонено CSRF-щитом (не-JSON Content-Type, нет Origin или он чужой).
ERR_STAFF_CSRF_REJECTED = "ERR-AUTH-009"

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Очередь: смотреть/взять/готово/отменить может любая роль (мини-матрица §3.2).
_any_role: Final = staff_auth.require_role(
    StaffRole.STAFF, StaffRole.RECEPTIONIST, StaffRole.MANAGER
)


class CompleteBody(BaseModel):
    """«Готово»: примечание опционально (частичное выполнение, spec 0021/0030)."""

    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _strip_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class CancelBody(BaseModel):
    """«Отменить»: причина обязательна (spec 0033 §5) — пустая строка не проходит."""

    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("cancellation note must not be blank")
        return stripped


def _require_json_same_origin(request: Request) -> None:
    """CSRF-щит JSON-действий (см. докстринг модуля); нарушение — 403 в конверте.

    В отличие от HTML-форм (`router._is_cross_origin`) отсутствующий Origin
    ЗДЕСЬ отказ: fetch шлёт Origin на POST всегда, а не-браузерным клиентам
    в кабинете делать нечего (сервисный API — `/api/v1/*` с токеном).
    """
    content_type = request.headers.get("content-type", "")
    origin = request.headers.get("origin", "")
    same_origin = bool(origin) and urlsplit(origin).netloc == request.headers.get("host", "")
    if content_type.lower().partition(";")[0].strip() != "application/json" or not same_origin:
        logger.warning(
            "staff.action_csrf_rejected",
            content_type=content_type,
            origin=origin,
        )
        raise AppError(
            code=ERR_STAFF_CSRF_REJECTED,
            message="Staff action requires application/json and a same-origin Origin header",
            status_code=403,
        )


async def _authorized_actor(request: Request) -> staff_auth.StaffContext:
    _require_json_same_origin(request)
    return await _any_role(request)


def _acting_user(context: staff_auth.StaffContext) -> requests_api.ActingUser:
    return requests_api.ActingUser(user_id=context.user_id, display_name=context.display_name)


@router.post("/requests/{request_id}/claim", summary="Взять заявку в работу")
async def claim_request(request: Request, request_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    """`new → in_progress`, пишет «кто взял» (claimed_by, spec 0033 §5).

    Тела у действия нет (JS шлёт `{}` ради Content-Type из CSRF-контракта);
    конфликт двух взявших разрешает карта переходов — второй получает 409.
    """
    context = await _authorized_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.IN_PROGRESS,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=_acting_user(context),
    )
    logger.info(
        "staff.request_claimed",
        request_id=str(request_id),
        user_id=str(context.user_id),
    )
    return updated


@router.post("/requests/{request_id}/complete", summary="Заявка выполнена")
async def complete_request(
    request: Request, request_id: uuid.UUID, body: CompleteBody
) -> requests_api.ServiceRequestRead:
    context = await _authorized_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.DONE,
        resolution_note=body.note,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=_acting_user(context),
    )
    logger.info(
        "staff.request_completed",
        request_id=str(request_id),
        user_id=str(context.user_id),
        with_note=body.note is not None,
    )
    return updated


@router.post("/requests/{request_id}/cancel", summary="Отменить заявку (причина обязательна)")
async def cancel_request(
    request: Request, request_id: uuid.UUID, body: CancelBody
) -> requests_api.ServiceRequestRead:
    context = await _authorized_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.CANCELLED,
        resolution_note=body.note,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=_acting_user(context),
    )
    logger.info(
        "staff.request_cancelled",
        request_id=str(request_id),
        user_id=str(context.user_id),
    )
    return updated
