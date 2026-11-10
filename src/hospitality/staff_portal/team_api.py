"""JSON-действия «Сотрудников» (spec 0033 §7, PR F): пригласить, отозвать ссылку,
сменить уровень и службы, отключить — только менеджер; суперадмину — ссылка
нового пароля и стирание учётки (spec 0044). Зовёт `static/team.js`;
«Пригласить списком» — `team_invites_api.py`.

Канон — `queue_api.py`: CSRF-щит `require_json_same_origin` и `require_role` напрямую.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from hospitality.platform import staff_auth, staff_invites, staff_superadmin, staff_team
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.staff_portal import team
from hospitality.staff_portal.browser import require_json_same_origin

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Состав команды — только менеджер (мини-матрица §3.2, развилка Ф-3 spec 0033).
_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


class InviteBody(BaseModel):
    """«Пригласить»: имя (его увидит сам сотрудник), логин, уровень и службы.

    Формат и свободу логина проверяет `staff_invites.create_invite`
    (ERR-AUTH-012, spec 0037 §4), службы — `team.normalized_service_keys`
    (ERR-AUTH-015, spec 0045 §6); здесь только потолки размера тела."""

    invited_name: str = Field(min_length=1, max_length=255)
    login: str = Field(max_length=64)
    role_key: StaffRole
    service_keys: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("invited_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("invited name must not be blank")
        return stripped


class AccessBody(BaseModel):
    """«Изменить»: уровень и службы членства в этом отеле — одной формой (spec 0045 §6)."""

    role_key: StaffRole
    service_keys: list[str] = Field(default_factory=list, max_length=50)


class InviteLinkView(BaseModel):
    """Свежая ссылка-приглашение: показывается ровно один раз (в БД — хэш).
    `login` — нормализованный логин сотрудника, как он будет входить."""

    invite_url: str
    invited_name: str
    login: str
    expires_in_hours: int


async def _authorized_manager(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
    return await _manager_role(request)


@router.post("/team/invites", summary="Пригласить сотрудника (одноразовая ссылка)")
async def create_team_invite(request: Request, body: InviteBody) -> InviteLinkView:
    """Ссылку менеджер передаёт сам (WhatsApp/лично) — email-порта нет
    (spec 0033 §3.4). Старое приглашение того же человека не гасится
    автоматически: увидев две строки в списке, менеджер отзывает лишнюю."""
    context = await _authorized_manager(request)
    service_keys = await team.normalized_service_keys(body.role_key, body.service_keys)
    grant = await staff_invites.create_invite(
        context.tenant_id,
        body.role_key,
        body.invited_name,
        body.login,
        invited_by=context.user_id,
        service_keys=service_keys,
    )
    return InviteLinkView(
        invite_url=team.invite_url(grant.invite_token),
        invited_name=body.invited_name,
        login=grant.login,
        expires_in_hours=get_settings().staff_invite_ttl_hours,
    )


@router.post("/team/invites/{invite_id}/revoke", summary="Отозвать приглашение")
async def revoke_team_invite(request: Request, invite_id: uuid.UUID) -> dict[str, bool]:
    context = await _authorized_manager(request)
    await staff_invites.revoke_invite(
        invite_id, tenant_id=context.tenant_id, actor_user_id=context.user_id
    )
    return {"ok": True}


@router.post("/team/members/{user_id}/access", summary="Сменить уровень и службы сотрудника")
async def change_team_member_access(
    request: Request, user_id: uuid.UUID, body: AccessBody
) -> dict[str, bool]:
    """Действует со следующего запроса сотрудника; свой уровень менеджер не
    меняет (ERR-AUTH-011) — иначе последний менеджер запер бы себе отель."""
    context = await _authorized_manager(request)
    service_keys = await team.normalized_service_keys(body.role_key, body.service_keys)
    await staff_team.change_member_access(
        user_id,
        context.tenant_id,
        body.role_key,
        service_keys=service_keys,
        actor_user_id=context.user_id,
    )
    return {"ok": True}


@router.post("/team/members/{user_id}/deactivate", summary="Отключить сотрудника")
async def deactivate_team_member(request: Request, user_id: uuid.UUID) -> dict[str, bool]:
    """Сессии гаснут немедленно, членства отзываются (spec 0033 §3.3).
    Реактивации в v1 нет — вернувшийся сотрудник заводится заново."""
    context = await _authorized_manager(request)
    await staff_team.deactivate_member(user_id, context.tenant_id, actor_user_id=context.user_id)
    return {"ok": True}


class PasswordResetLinkView(BaseModel):
    """Свежая ссылка нового пароля: показывается ровно один раз (в БД — хэш)."""

    reset_url: str
    login: str
    expires_in_hours: int


async def _authorized_superadmin(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
    return await staff_superadmin.require_superadmin(request)


@router.post(
    "/team/members/{user_id}/password-reset",
    summary="Суперадмин: ссылка нового пароля сотруднику (spec 0044 §4)",
)
async def reset_team_member_password(request: Request, user_id: uuid.UUID) -> PasswordResetLinkView:
    """Только суперадмин (не он — 403 ERR-AUTH-003). До использования ссылки
    ничего не меняется: старый пароль работает, сессии живы."""
    context = await _authorized_superadmin(request)
    grant = await staff_superadmin.issue_password_reset(
        user_id, context.tenant_id, actor_user_id=context.user_id
    )
    return PasswordResetLinkView(
        reset_url=team.reset_url(grant.reset_token),
        login=grant.login,
        expires_in_hours=get_settings().staff_password_reset_ttl_hours,
    )


@router.post("/team/members/{user_id}/erase", summary="Суперадмин: стереть учётку (spec 0044 §5)")
async def erase_team_member(request: Request, user_id: uuid.UUID) -> dict[str, bool]:
    """Только суперадмин. Отключение — в том же шаге; восстановления нет."""
    context = await _authorized_superadmin(request)
    await staff_superadmin.erase_member(user_id, context.tenant_id, actor_user_id=context.user_id)
    return {"ok": True}
