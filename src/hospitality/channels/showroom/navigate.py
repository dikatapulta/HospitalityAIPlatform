"""Маршруты помощника 3D-витрины: `POST …/navigate` (spec 0069 §2.3, §4).

Тело `{text ≤ 300, language?, audience}` → `{commands, reply, source}`; ход —
`ai/showroom/navigate.py`. До хода — две ступени лимита (канон spec 0023, ступень
по адресу — spec 0027 §3.3, адрес — `shared/clientip.py`): 30 фраз за 10 минут с
адреса и 600 в сутки отеля, правила и модель вместе; выше — 429 `ERR-SHOWROOM-003`.
Деньги держит бюджет назначения `showroom`, лимит — шум и перебор. Фраза не
хранится и в лог не пишется: в ней может оказаться то, что посетитель зря написал.
"""

from __future__ import annotations

from datetime import tzinfo
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from hospitality.ai.showroom import navigate as assistant
from hospitality.ai.showroom.navigate import MAX_TEXT_LENGTH, Source
from hospitality.ai.tools.navigate_showroom import Audience
from hospitality.modules.showroom.api import SceneDocument
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

ERR_SHOWROOM_RATE_LIMITED: Final = "ERR-SHOWROOM-003"

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


async def answer(
    body: NavigateRequest,
    *,
    scene: SceneDocument,
    zone: tzinfo,
    assistant_enabled: bool,
    client_ip: str,
) -> NavigateResponse:
    """Лимиты, затем ход помощника (внутри `tenant_context`).

    `zone` — пояс отеля из конфига тенанта: «сегодня» для черновика дат (FOUNDATION §9).
    """
    await _consume(NAVIGATE_IP_SCOPE, client_ip, NAVIGATE_IP_LIMIT, NAVIGATE_IP_WINDOW_SECONDS)
    await _consume(
        NAVIGATE_TENANT_SCOPE,
        str(current_tenant_id()),
        NAVIGATE_TENANT_LIMIT,
        NAVIGATE_TENANT_WINDOW_SECONDS,
    )
    result = await assistant.navigate(
        body.text,
        scene=scene,
        audience=body.audience,
        language=body.language[:2].lower(),
        today=utc_now().astimezone(zone).date(),
        assistant_enabled=assistant_enabled,
    )
    return NavigateResponse(commands=result.commands, reply=result.reply, source=result.source)


async def _consume(scope: str, key: str, limit: int, window_seconds: int) -> None:
    """Ступень лимита; ключ в лог не пишется — у ступени по адресу это адрес посетителя."""
    decision = await consume_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
    if decision.available and not decision.allowed:
        if decision.first_rejection:
            logger.warning(
                "showroom_navigate_rate_limited",
                error_code=ERR_SHOWROOM_RATE_LIMITED,
                scope=scope,
                limit=limit,
            )
        raise AppError(
            code=ERR_SHOWROOM_RATE_LIMITED,
            message="Too many assistant requests — try again later",
            status_code=429,
        )
