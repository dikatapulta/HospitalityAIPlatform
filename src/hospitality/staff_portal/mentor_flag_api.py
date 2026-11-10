"""Действие «Неточность в статье» (spec 0072 §6, вариант (б); #898).

Канон `mentor_api.py`: CSRF-щит `browser.require_json_same_origin`, личность —
`mentor.any_role` (любой уровень, docs/RBAC.md), раздел выключен у отеля — 404. Отметка —
только своя: `user_id` вошедшего, чужую не поставить и не снять. Скрипт —
`static/mentor_flags.js`; тексты и счётчик — `mentor_flags.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.mentor import api as mentor_api
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import mentor
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api/mentor/flags", tags=["staff-portal"])


class FlagBody(BaseModel):
    """Номер статьи («14», «14-1») и что сделать: отметить или снять."""

    model_config = ConfigDict(extra="forbid")

    article_number: str = Field(pattern=r"^\d{1,6}(-\d{1,6})?$", max_length=16)
    flagged: bool


class FlagView(BaseModel):
    flagged: bool


@router.post("")
async def flag_article(request: Request, body: FlagBody) -> FlagView:
    """Отметить или снять; статьи нет в нынешнем кодексе — `flagged: false`, без записи."""
    require_json_same_origin(request)
    staff = await mentor.any_role(request)
    await mentor.require_section(staff)
    flagged = await mentor_api.set_article_flag(
        staff.user_id, body.article_number, flagged=body.flagged
    )
    # Без `user_id`: по `correlation_id` он связал бы отметку с сотрудником (PII_REGISTRY).
    logger.info("mentor_article_flag_set", article_number=body.article_number, flagged=flagged)
    return FlagView(flagged=flagged)
