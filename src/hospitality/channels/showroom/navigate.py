"""Помощник 3D-витрины: `POST /showroom/{slug}/navigate` (spec 0069 §2.3, §4).

Тело `{text ≤ 300, language, audience}` → `{commands, reply, source}`; ход —
`ai/showroom/navigate.py`. Допуск — как у сцены (`access`): витрина не видна снаружи —
404 `001`, нет доступа — 403 `005`. Кабинет зовёт ту же `answer` под своей сессией
(`staff_portal/showroom_api.py`).

До хода — две ступени лимита (канон spec 0023, ступень по адресу — spec 0027 §3.3,
адрес — `shared/clientip.py`): 30 фраз за 10 минут с адреса и 600 в сутки отеля,
правила и модель вместе; выше — 429 `ERR-SHOWROOM-003`. Деньги держит бюджет
назначения `showroom`, лимит — шум и перебор. Фраза не хранится и в лог не пишется:
в ней может оказаться то, что посетитель зря написал.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from hospitality.ai.showroom import navigate as assistant
from hospitality.ai.showroom.navigate import MAX_TEXT_LENGTH, Source
from hospitality.ai.tools.navigate_showroom import Audience
from hospitality.channels.showroom import access
from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.config import load_tenant_config
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.shared.clientip import client_ip
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError, ErrorResponse
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import current_tenant_id, tenant_context

logger = get_logger(module=__name__)

router = APIRouter()

NAVIGATE_IP_SCOPE: Final = "showroom_navigate_ip"
NAVIGATE_IP_LIMIT: Final = 30
NAVIGATE_IP_WINDOW_SECONDS: Final = 600
NAVIGATE_TENANT_SCOPE: Final = "showroom_navigate_tenant"
NAVIGATE_TENANT_LIMIT: Final = 600
NAVIGATE_TENANT_WINDOW_SECONDS: Final = 86_400


class NavigateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    language: str = Field(default="en", max_length=8)
    audience: Audience = "group"


class NavigateResponse(BaseModel):
    commands: list[dict[str, Any]]
    reply: str
    source: Source


@router.post(
    "/{tenant_slug}/navigate",
    responses={code: {"model": ErrorResponse} for code in (403, 404, 429)},
)
async def navigate(request: Request, tenant_slug: str, body: NavigateRequest) -> NavigateResponse:
    """Фраза посетителя страницы витрины (§2.3)."""
    showroom = await access.open_showroom(tenant_slug)
    access.require_access(showroom, request)
    with tenant_context(showroom.tenant_id):
        return await answer(body, settings=showroom.settings, client_ip=client_ip(request))


async def answer(
    body: NavigateRequest, *, settings: ShowroomConfig, client_ip: str
) -> NavigateResponse:
    """Лимиты, затем ход помощника по сцене отеля (внутри `tenant_context`).

    «Сегодня» для черновика дат — в поясе отеля из его конфига (FOUNDATION §9).
    Сцены нет — 404 `001`, как у страницы.
    """
    await _consume(NAVIGATE_IP_SCOPE, client_ip, NAVIGATE_IP_LIMIT, NAVIGATE_IP_WINDOW_SECONDS)
    await _consume(
        NAVIGATE_TENANT_SCOPE,
        str(current_tenant_id()),
        NAVIGATE_TENANT_LIMIT,
        NAVIGATE_TENANT_WINDOW_SECONDS,
    )
    current = await showroom_api.get_scene()
    if current is None:
        raise AppError(code=showroom_api.ERR_SHOWROOM_DISABLED, message="No scene", status_code=404)
    async with session_scope() as session:
        config = await load_tenant_config(session, current_tenant_id())
    result = await assistant.navigate(
        body.text,
        scene=current.scene,
        audience=body.audience,
        language=body.language[:2].lower(),
        today=utc_now().astimezone(config.tzinfo).date(),
        assistant_enabled=settings.assistant_enabled,
    )
    return NavigateResponse(commands=result.commands, reply=result.reply, source=result.source)


async def _consume(scope: str, key: str, limit: int, window_seconds: int) -> None:
    """Ступень лимита; ключ в лог не пишется — у ступени по адресу это адрес посетителя."""
    decision = await consume_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
    if decision.available and not decision.allowed:
        if decision.first_rejection:
            logger.warning(
                "showroom_navigate_rate_limited",
                error_code=showroom_api.ERR_SHOWROOM_RATE_LIMITED,
                scope=scope,
                limit=limit,
            )
        raise AppError(
            code=showroom_api.ERR_SHOWROOM_RATE_LIMITED,
            message="Too many assistant requests — try again later",
            status_code=429,
        )
