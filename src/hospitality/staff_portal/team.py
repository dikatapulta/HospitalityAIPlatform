"""Данные страницы «Сотрудники» (spec 0033 §7, PR F серии #48; spec 0037 §5).

Собирает контекст `team.html`: список членств тенанта (имя, логин, уровень,
службы, статус, активность), форма приглашения с уровнем и службами (spec 0045
§6), ожидающие ссылки и «как войти» —
общий адрес входа, код отеля и прямая ссылка на кабинет. Под суперадмином —
кнопки «Сбросить пароль» и «Стереть учётку» (spec 0044 §4–§5). Действия (пригласить,
отозвать, сменить уровень и службы, отключить) — `api_router.py`; маршрут страницы —
`router.py`. Здесь только чтение через `platform/` (R-5) и подписи для UI.

Здесь же живут подписи уровней всего кабинета (`role_label`): уровень — понятие
состава команды, а показывают его и выбор отеля, и главная, и этот список —
один словарь на все три места (P-12). Службы — категории заявок отеля
(spec 0045 §2): их названия страница берёт у модуля requests.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_invites, staff_team
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
from hospitality.platform.staff_auth import StaffContext
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY, normalize_service_keys
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now

# Часовой пояс отеля и формат локального времени — общий помощник кабинета
# (`checkin.py`): третьей копии правила «конфиг тенанта → пояс, иначе UTC»
# в пакете быть не должно.
from hospitality.staff_portal.checkin import format_local, hotel_zone

ROLE_LABELS: Final[dict[StaffRole, str]] = {
    StaffRole.STAFF: "Сотрудник",
    StaffRole.SENIOR: "Старший службы",
    StaffRole.MANAGER: "Менеджер отеля",
}

# Что уровень открывает — подсказка под выбором в форме приглашения: менеджер
# выбирает уровень раз в жизни сотрудника и не обязан помнить матрицу
# docs/RBAC.md. Сводка и журнал старшего — PR 3 spec 0045, отсюда «получит».
ROLE_DESCRIPTIONS: Final[dict[StaffRole, str]] = {
    StaffRole.STAFF: "заявки своих служб.",
    StaffRole.SENIOR: ("заявки своих служб; сводку и журнал службы получит следующим обновлением."),
    StaffRole.MANAGER: "все заявки, сотрудники, справочник и сводка.",
}


def role_label(role: StaffRole) -> str:
    # Фолбэк на value: новый уровень без перевода не должен ронять страницу 500-кой.
    return ROLE_LABELS.get(role, role.value)


def role_choices() -> list[dict[str, str]]:
    """Уровни для формы приглашения и правки — в порядке возрастания прав."""
    return [
        {"key": role.value, "label": role_label(role), "description": ROLE_DESCRIPTIONS[role]}
        for role in (StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)
    ]


def service_choices(
    categories: list[requests_api.RequestCategoryRead],
) -> list[dict[str, str]]:
    """Службы отеля для галочек: ресепшен первым — у него особое право, остальные
    в порядке `list_categories` (по ключу), как чипсы очереди."""
    ordered = sorted(categories, key=lambda category: category.key != RECEPTION_SERVICE_KEY)
    return [{"key": category.key, "name": category.name} for category in ordered]


def services_label(service_keys: list[str], names: dict[str, str]) -> str:
    """«Уборка и бельё · Прачечная». Ключ без категории (её удалили руками)
    показывается как есть: менеджер должен увидеть, что служба странная."""
    return " · ".join(names.get(key, key) for key in service_keys)


async def normalized_service_keys(role: StaffRole, service_keys: list[str]) -> list[str]:
    """Службы из формы — к записи, по категориям ЭТОГО отеля (spec 0045 §6).

    Категории читает кабинет (контекст тенанта запроса), правило — у ядра
    (`staff_services.normalize_service_keys`, ERR-AUTH-015): ядро модуль
    requests не импортирует (R-5).
    """
    known = {category.key for category in await requests_api.list_categories()}
    return normalize_service_keys(role, service_keys, known_service_keys=known)


def _public_url(path: str) -> str:
    """Абсолютный URL кабинета (канон `checkin.bind_link_url`): ссылки
    менеджер пересылает сам — в них обязан быть полный адрес инсталляции."""
    return f"{get_settings().public_base_url.rstrip('/')}{path}"


def invite_url(token: str) -> str:
    return _public_url(f"/staff/invite/{token}")


def reset_url(token: str) -> str:
    """Ссылка нового пароля (spec 0044 §4) — абсолютная, как приглашение."""
    return _public_url(f"/staff/reset/{token}")


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
    member: staff_team.TenantMemberView,
    staff: StaffContext,
    now: datetime,
    service_names: dict[str, str],
) -> dict[str, Any]:
    active = (
        member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
    )
    is_self = member.user_id == staff.user_id
    # Строку суперадмина не трогает никто (ERR-AUTH-013, spec 0044 §2); его
    # две кнопки — у остальных строк и только под учёткой суперадмина.
    touchable = not is_self and not member.is_platform_admin
    superadmin = staff.is_platform_admin and touchable
    return {
        "user_id": str(member.user_id),
        "display_name": member.display_name,
        "login": member.login,
        "role_key": member.role_key.value,
        "role_label": role_label(member.role_key),
        "service_keys": member.service_keys,
        "services_label": services_label(member.service_keys, service_names),
        # Сотрудник и старший без служб — переходное состояние после миграции
        # 0033 (spec 0045 §7): с разделением очереди такой не увидит заявок.
        "needs_services": active
        and member.role_key is not StaffRole.MANAGER
        and not member.service_keys,
        "status_label": _status_label(member),
        "last_active_label": _last_active_label(member.last_active_at, now),
        "active": active,
        "is_self": is_self,
        "is_superadmin_row": member.is_platform_admin,
        # Себя менеджер не трогает (ERR-AUTH-011): кнопок нет вовсе, чтобы
        # отказ не выглядел поломкой. Отключённого — тоже: реактивации в v1 нет.
        "can_manage": active and touchable,
        # Сбросить можно активному с логином, стереть — любого (spec 0044 §4–§5).
        "can_reset": superadmin and active and member.login is not None,
        "can_erase": superadmin,
    }


def _invite_row(
    invite: staff_invites.PendingInviteView, zone: tzinfo, service_names: dict[str, str]
) -> dict[str, Any]:
    return {
        "invite_id": str(invite.invite_id),
        "invited_name": invite.invited_name,
        "login": invite.login,
        "role_label": role_label(invite.role_key),
        "services_label": services_label(invite.service_keys, service_names),
        "expires_local": format_local(invite.expires_at, zone),
    }


async def build_team_context(staff: StaffContext) -> dict[str, Any]:
    """Контекст страницы «Сотрудники» (уровень `manager`, docs/RBAC.md).

    Читает платформенные таблицы через `platform/` — они вне RLS (ADR-008 §6),
    тенантную границу держит явный `tenant_id` из `StaffContext`.
    """
    members = await staff_team.list_tenant_members(staff.tenant_id)
    invites = await staff_invites.list_pending_invites(staff.tenant_id)
    categories = await requests_api.list_categories()
    service_names = {category.key: category.name for category in categories}
    zone = await hotel_zone(staff)
    now = utc_now()
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "members": [_member_row(member, staff, now, service_names) for member in members],
        "invites": [_invite_row(invite, zone, service_names) for invite in invites],
        "role_choices": role_choices(),
        "service_choices": service_choices(categories),
        "manager_role_key": StaffRole.MANAGER.value,
        "invite_ttl_hours": get_settings().staff_invite_ttl_hours,
        "is_superadmin": staff.is_platform_admin,
        "reset_ttl_hours": get_settings().staff_password_reset_ttl_hours,
        "actions_endpoint": f"/staff/{staff.tenant_slug}/api/team",
        # «Как войти» (spec 0037 §5): общий вход + код отеля и прямая ссылка —
        # без сессии она ведёт на вход отеля, с сессией — в кабинет.
        "staff_entry_url": _public_url("/staff"),
        "hotel_link": _public_url(f"/staff/{staff.tenant_slug}"),
    }
