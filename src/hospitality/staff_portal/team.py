"""Данные страницы «Сотрудники» (spec 0033 §7, PR F серии #48).

Собирает контекст `team.html`: список членств тенанта (имя, роль, статус,
активность), форма приглашения и ожидающие ссылки. Действия (пригласить,
отозвать, сменить роль, отключить) — `api_router.py`; маршрут страницы —
`router.py`. Здесь только чтение через `platform/` (R-5) и подписи для UI.

Здесь же живут подписи ролей всего кабинета (`role_label`): роль — понятие
состава команды, а показывают её и выбор отеля, и главная, и этот список —
один словарь на все три места (P-12).
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any, Final

from hospitality.platform import staff_invites, staff_team
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now

# Часовой пояс отеля и формат локального времени — общий помощник кабинета
# (`checkin.py`): третьей копии правила «конфиг тенанта → пояс, иначе UTC»
# в пакете быть не должно.
from hospitality.staff_portal.checkin import format_local, hotel_zone

ROLE_LABELS: Final[dict[StaffRole, str]] = {
    StaffRole.STAFF: "Сотрудник",
    StaffRole.RECEPTIONIST: "Ресепшен",
    StaffRole.MANAGER: "Менеджер",
}

# Что роль открывает — подсказка под кнопкой в форме приглашения: менеджер
# выбирает роль раз в жизни сотрудника и не обязан помнить мини-матрицу §3.2
# (полная — docs/RBAC.md).
ROLE_DESCRIPTIONS: Final[dict[StaffRole, str]] = {
    StaffRole.STAFF: "Очередь заявок",
    StaffRole.RECEPTIONIST: "Очередь заявок и заселение",
    StaffRole.MANAGER: "Всё, включая сотрудников",
}


def role_label(role: StaffRole) -> str:
    # Фолбэк на value: новая роль без перевода не должна ронять страницу 500-кой.
    return ROLE_LABELS.get(role, role.value)


def role_choices() -> list[dict[str, str]]:
    """Роли для формы приглашения и смены роли — в порядке возрастания прав."""
    return [
        {"key": role.value, "label": role_label(role), "description": ROLE_DESCRIPTIONS[role]}
        for role in (StaffRole.STAFF, StaffRole.RECEPTIONIST, StaffRole.MANAGER)
    ]


def invite_url(token: str) -> str:
    """Абсолютный URL приглашения (канон `checkin.bind_link_url`): ссылку
    менеджер пересылает сам — в ней обязан быть полный адрес инсталляции."""
    return f"{get_settings().public_base_url.rstrip('/')}/staff/invite/{token}"


def _status_label(member: staff_team.TenantMemberView) -> str:
    """Одна подпись на две величины (членство и учётка) — см. `TenantMemberView`."""
    if member.user_status is not UserStatus.ACTIVE:
        return "отключён"
    if member.membership_status is not MembershipStatus.ACTIVE:
        return "доступ отозван"
    return "активен"


def _last_active_label(moment: datetime | None, now: datetime) -> str:
    """Активность крупными мазками: менеджеру важно «заходит / не заходит»,
    а не минуты (минутный возраст — у карточек очереди)."""
    if moment is None:
        return "не заходил"
    days = (now - moment).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    return f"{days} дн назад"


def _member_row(
    member: staff_team.TenantMemberView, staff: StaffContext, now: datetime
) -> dict[str, Any]:
    active = (
        member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
    )
    is_self = member.user_id == staff.user_id
    return {
        "user_id": str(member.user_id),
        "display_name": member.display_name,
        "role_key": member.role_key.value,
        "role_label": role_label(member.role_key),
        "status_label": _status_label(member),
        "last_active_label": _last_active_label(member.last_active_at, now),
        "active": active,
        "is_self": is_self,
        # Себя менеджер не трогает (ERR-AUTH-011): кнопок нет вовсе, чтобы
        # отказ не выглядел поломкой. Отключённого — тоже: реактивации в v1 нет.
        "can_manage": active and not is_self,
    }


def _invite_row(invite: staff_invites.PendingInviteView, zone: tzinfo) -> dict[str, Any]:
    return {
        "invite_id": str(invite.invite_id),
        "invited_name": invite.invited_name,
        "role_label": role_label(invite.role_key),
        "expires_local": format_local(invite.expires_at, zone),
    }


async def build_team_context(staff: StaffContext) -> dict[str, Any]:
    """Контекст страницы «Сотрудники» (роль `manager`, мини-матрица §3.2).

    Читает платформенные таблицы через `platform/` — они вне RLS (ADR-008 §6),
    тенантную границу держит явный `tenant_id` из `StaffContext`.
    """
    members = await staff_team.list_tenant_members(staff.tenant_id)
    invites = await staff_invites.list_pending_invites(staff.tenant_id)
    zone = await hotel_zone(staff)
    now = utc_now()
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "members": [_member_row(member, staff, now) for member in members],
        "invites": [_invite_row(invite, zone) for invite in invites],
        "role_choices": role_choices(),
        "invite_ttl_hours": get_settings().staff_invite_ttl_hours,
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/team",
    }
