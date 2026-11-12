"""«Пригласить списком» на странице «Сотрудники» (spec 0072 §4, PR 2 серии #898).

Отдельно от `team_api.py`: там одиночное приглашение и остальной состав
команды. Права и щит — те же: только менеджер (`staff_auth.require_role`), CSRF —
`browser.require_json_same_origin`. Проверку списка и выпуск делает
`platform/staff_invite_batch.create_invites` — весь список или ни одного.

Ответ — данные листа для печати: по карточке на человека имя, логин и QR
одноразовой ссылки (`checkin.qr_svg`), один срок по часам отеля. Ссылки видны
только в этом ответе (в БД — хэши): `Cache-Control: no-store`, саму ссылку текстом
ответ не несёт — только картинкой, которую печатают. Карточку рисует `team_batch.js`
по шаблону из `team.html`.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field, field_validator

from hospitality.platform import staff_auth, staff_invite_batch
from hospitality.platform.models import StaffRole
from hospitality.staff_portal import team
from hospitality.staff_portal.browser import require_json_same_origin
from hospitality.staff_portal.checkin import hotel_zone, qr_svg

router = APIRouter(prefix="/{tenant_slug}/api/team", tags=["staff-portal"])

_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


class BatchRowBody(BaseModel):
    """Строка списка: имя и логин; формат и свободу логина проверяет домен."""

    invited_name: str = Field(min_length=1, max_length=255)
    login: str = Field(max_length=64)

    @field_validator("invited_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("invited name must not be blank")
        return stripped


class BatchInviteBody(BaseModel):
    """«Выпустить приглашения»: одни уровень и службы на весь список, до 100 строк."""

    role_key: StaffRole
    service_keys: list[str] = Field(default_factory=list, max_length=50)
    rows: list[BatchRowBody] = Field(min_length=1, max_length=staff_invite_batch.MAX_BATCH_INVITES)


class InviteCardView(BaseModel):
    """Карточка листа: имя, логин и QR ссылки — SVG от segno, вставляется как есть."""

    invited_name: str
    login: str
    qr_svg: str


class InviteSheetView(BaseModel):
    """Лист для печати: срок — «13.11, 18:00» по часам отеля, один на весь лист."""

    expires_local: str
    cards: list[InviteCardView]


@router.post("/invites/batch", summary="Пригласить сотрудников списком (лист для печати)")
async def create_team_invites(
    request: Request, body: BatchInviteBody, response: Response
) -> InviteSheetView:
    require_json_same_origin(request)
    context = await _manager_role(request)
    service_keys = await team.normalized_service_keys(body.role_key, body.service_keys)
    grants = await staff_invite_batch.create_invites(
        context.tenant_id,
        body.role_key,
        [
            staff_invite_batch.InviteRow(invited_name=row.invited_name, login=row.login)
            for row in body.rows
        ],
        invited_by=context.user_id,
        service_keys=service_keys,
        actor=context.actor(),
    )
    response.headers["Cache-Control"] = "no-store"
    zone = await hotel_zone(context)
    # Сроки строк расходятся на доли секунды: на листе — самый ранний.
    expires_at = min(grant.expires_at for grant in grants)
    return InviteSheetView(
        expires_local=expires_at.astimezone(zone).strftime("%d.%m, %H:%M"),
        cards=[
            InviteCardView(
                invited_name=row.invited_name,
                login=grant.login,
                qr_svg=qr_svg(team.invite_url(grant.invite_token)),
            )
            for row, grant in zip(body.rows, grants, strict=True)
        ],
    )
