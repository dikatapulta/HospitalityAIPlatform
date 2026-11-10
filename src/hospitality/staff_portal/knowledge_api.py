"""JSON-действия справочника отеля (spec 0036 §7, issue #335): добавить, изменить
и удалить факт — только менеджер; зовёт `static/knowledge.js`.

Канон — `queue_api.py`: CSRF-щит `require_json_same_origin` и `require_role` напрямую.
Все три действия — POST с темой В ТЕЛЕ: тема — свободный текст, а «/» в пути
даёт 404 даже процентно-кодированным, и такой факт стал бы нередактируемым.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel

from hospitality.platform import staff_auth
from hospitality.platform.config import TenantConfig, mutate_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import knowledge
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Справочник — только менеджер (docs/RBAC.md).
_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


class HotelFactBody(BaseModel):
    """Факт из формы. Пределов схема НЕ несёт намеренно (§7): их проверяет
    `knowledge` под блокировкой и отвечает надписью §8.1, а предел здесь дал бы
    422 `ERR-PLATFORM-002` — один рамочный текст на десять причин."""

    topic: str
    answer: str
    valid_until: date | None = None


class HotelFactUpdateBody(HotelFactBody):
    """`topic` — какой факт правится, `new_topic` — новая тема (переименование)."""

    new_topic: str | None = None


class HotelFactDeleteBody(BaseModel):
    topic: str


async def _edit_hotel_facts(
    request: Request, mutate: Callable[[TenantConfig], TenantConfig], event: str, topic: str
) -> dict[str, bool]:
    require_json_same_origin(request)
    context = await _manager_role(request)
    async with platform_session_scope() as session:
        await mutate_tenant_config(session, context.tenant_id, mutate)
    # Тема — не ПД (данные отеля, spec 0036 §9); actor обязателен (spec 0033 §9).
    logger.info(event, topic=topic, user_id=str(context.user_id))
    return {"ok": True}


@router.post("/knowledge/facts", summary="Справочник: добавить факт")
async def add_hotel_fact(request: Request, body: HotelFactBody) -> dict[str, bool]:
    return await _edit_hotel_facts(
        request,
        knowledge.adding(body.topic, body.answer, body.valid_until),
        "staff.hotel_fact_created",
        body.topic.strip(),
    )


@router.post("/knowledge/facts/update", summary="Справочник: изменить факт")
async def update_hotel_fact(request: Request, body: HotelFactUpdateBody) -> dict[str, bool]:
    return await _edit_hotel_facts(
        request,
        knowledge.updating(body.topic, body.new_topic, body.answer, body.valid_until),
        "staff.hotel_fact_updated",
        (body.new_topic or body.topic).strip(),
    )


@router.post("/knowledge/facts/delete", summary="Справочник: удалить факт")
async def delete_hotel_fact(request: Request, body: HotelFactDeleteBody) -> dict[str, bool]:
    return await _edit_hotel_facts(
        request, knowledge.deleting(body.topic), "staff.hotel_fact_deleted", body.topic.strip()
    )
