"""Действия раздела «Предложить идею»: ход, «Отправить разработчику», «Начать заново»,
«Удалить», «Разобрано», выгрузка (spec 0071 §3, §8; #871, PR 3 серии).

Канон `mentor_api.py`: CSRF-щит `browser.require_json_same_origin`, личность —
`staff_auth.require_role` (интервью — старшему и менеджеру), «Разобрано» и выгрузка —
`staff_superadmin.require_superadmin`; раздел выключен у отеля — 404. Ход отвечает
тем же потоком `text/event-stream`, что «Наставник» (`mentor_api.stream_response`):
`delta`, затем `done` {answer, sources, draft} или `error` {code, message} с текстом
автору (§9). В `draft` — только id и пересказ: пометки ИИ автор не видит (§6).
`user_id` автора в логах этих маршрутов не пишется (как у «Наставника», PII_REGISTRY);
суперадмина — пишется (§8).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import StreamingResponse

from hospitality.ai.mentor import ideas as ai_ideas
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform import staff_superadmin
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import utc_now
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import ideas, ideas_all
from hospitality.staff_portal.browser import require_json_same_origin
from hospitality.staff_portal.mentor_api import (
    MentorStreamResponse,
    MessageBody,
    Providers,
    stream_response,
)

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api/ideas", tags=["staff-portal"])

RoleDependency = Callable[[Request], Awaitable[StaffContext]]
IdeaMode = Literal["idea", "interview"]


def _no_nul(value: str) -> str:
    """NUL Postgres в тексте не хранит (канон `mentor_api`, #531)."""
    if "\x00" in value:
        raise ValueError("нулевой символ в тексте")
    return value


class TurnBody(MessageBody):
    """Сообщение автора: режим и текст (пустой, длиннее предела или с NUL — 422)."""

    mode: IdeaMode


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: IdeaMode


class SendBody(BaseModel):
    """«Отправить разработчику»: черновик и пересказ из поля — как его поправил автор."""

    model_config = ConfigDict(extra="forbid")

    idea_id: uuid.UUID
    summary: str = Field(max_length=ideas.MAX_DRAFT_CHARS)

    @field_validator("summary")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = _no_nul(value).strip()
        if not text:
            raise ValueError("пустой текст")
        return text


async def _actor(request: Request, role: RoleDependency = ideas.any_role) -> StaffContext:
    """CSRF и личность — до всего; затем раздел у отеля: выключен — 404."""
    require_json_same_origin(request)
    staff = await role(request)
    await ideas.require_section(staff)
    return staff


@router.post("/turn", response_class=StreamingResponse)
async def turn(request: Request, body: TurnBody, providers: Providers) -> MentorStreamResponse:
    """Сообщение автора: вопрос модели или черновик карточки (§3–4)."""
    mode = mentor_api.ConversationMode(body.mode)
    staff = await _actor(request, ideas.mode_role(mode))
    return stream_response(
        ai_ideas.idea_turn(staff.user_id, mode, body.message, providers=providers),
        f"{mode.value}_turn",
        error_text=ideas.error_text,
        unavailable=ideas.UNAVAILABLE_TEXT,
    )


@router.post("/send")
async def send(request: Request, body: SendBody) -> dict[str, bool]:
    """«Отправить разработчику» (§3 п. 4): уже отправлена или стёрта — 409 ERR-MENTOR-005.

    Права режима — по самой карточке: модуль отдаёт только свою, а интервью у `staff`
    не бывает — ход интервью ему закрыт.
    """
    staff = await _actor(request)
    await mentor_api.send_idea(staff.user_id, body.idea_id, summary=body.summary)
    return {"ok": True}


@router.post("/reset")
async def reset(request: Request, body: ModeBody) -> dict[str, bool]:
    """«Начать заново» (§3 п. 7): открытый диалог режима и его черновик; отправленное — нет."""
    mode = mentor_api.ConversationMode(body.mode)
    staff = await _actor(request, ideas.mode_role(mode))
    deleted = await mentor_api.delete_conversation(staff.user_id, mode)
    logger.info("mentor_conversation_reset", mode=mode.value, deleted=deleted)
    return {"deleted": deleted}


@router.post("/mine/{idea_id}/delete")
async def delete_mine(request: Request, idea_id: uuid.UUID) -> dict[str, bool]:
    """«Удалить» в «Моих идеях»: карточка с разговором, только своя (§8)."""
    staff = await _actor(request)
    return {"deleted": await mentor_api.delete_my_idea(staff.user_id, idea_id)}


@router.post("/all/{idea_id}/reviewed")
async def reviewed(request: Request, idea_id: uuid.UUID) -> dict[str, bool]:
    """«Разобрано» (суперадмин, §8); карточки нет в отеле — 404."""
    staff = await _actor(request, staff_superadmin.require_superadmin)
    if not await mentor_api.mark_idea_reviewed(idea_id):
        raise HTTPException(status_code=404)
    logger.info("superadmin.idea_reviewed", user_id=str(staff.user_id), idea_id=str(idea_id))
    return {"ok": True}


@router.get("/export")
async def export(request: Request) -> Response:
    """«Скачать всё (Markdown)» (суперадмин, §8): один файл, не кэшируется.

    GET без тела — CSRF-щит JSON-действий здесь не нужен: чтение ничего не меняет,
    а чужой сайт ответа не прочитает (тот же источник, cookie `SameSite`).
    """
    staff = await staff_superadmin.require_superadmin(request)
    config = await ideas.require_section(staff)
    body = await ideas_all.export_markdown(staff, config.tzinfo)
    name = f"ideas-{staff.tenant_slug}-{utc_now().astimezone(config.tzinfo):%Y-%m-%d}.md"
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )
