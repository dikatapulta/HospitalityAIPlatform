"""Персонал демо-картины (#605): приглашения, личности шагов и этажи горничных.

Вход — только ссылками-приглашениями (канон `platform/staff_invites.py`): паролей
команда не придумывает и не печатает. Учётка с демо-логином уже есть — человек
вошёл, ссылка не нужна. Ждёт приглашение — оно гасится и выпускается новое: в БД
лежит только хэш ссылки, прежнюю показать нечем, а потерянный вывод терминала —
обычный повод запустить команду ещё раз.

Шаги картины («взял», «убрал», «принял») нажимают демо-сотрудники, уже принявшие
приглашение; до того — менеджер отеля, чтобы имя у шага было. Этажи раздаются
только вошедшим горничным: раздача хранит `user_id`, а у приглашения его нет.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
from hospitality.platform.staff_actions import StaffActor
from hospitality.platform.staff_invites import create_invite, list_pending_invites, revoke_invite
from hospitality.platform.staff_services import (
    HOUSEKEEPING_SERVICE_KEY,
    RECEPTION_SERVICE_KEY,
    normalize_service_keys,
)
from hospitality.platform.staff_team import TenantMemberView, list_tenant_members
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError


class DemoShowcaseError(Exception):
    """Отказ команды: понятный текст оператору вместо трассировки (канон checkin)."""


@dataclass(frozen=True)
class DemoMember:
    """Демо-сотрудник: логин, имя без настоящих людей, уровень, службы и этаж."""

    login: str
    name: str
    role: StaffRole
    service: str
    floor: int | None = None


RECEPTION_LOGIN: Final = "DEMOREC"
SENIOR_LOGIN: Final = "DEMOSENIOR"

DEMO_STAFF: Final = (
    DemoMember(RECEPTION_LOGIN, "Ресепшен Демо", StaffRole.STAFF, RECEPTION_SERVICE_KEY),
    DemoMember(SENIOR_LOGIN, "Старшая Демо", StaffRole.SENIOR, HOUSEKEEPING_SERVICE_KEY),
    DemoMember("DEMO15", "Горничная 15", StaffRole.STAFF, HOUSEKEEPING_SERVICE_KEY, floor=15),
    DemoMember("DEMO16", "Горничная 16", StaffRole.STAFF, HOUSEKEEPING_SERVICE_KEY, floor=16),
)

# Ресепшен приглашается службой (spec 0045 §2), а служба — категория заявок отеля:
# у демо-отеля сида её нет.
RECEPTION_CATEGORY: Final = requests_api.RequestCategoryCreate(
    key=RECEPTION_SERVICE_KEY, name="Ресепшен"
)


@dataclass(frozen=True)
class DemoActors:
    """Кто нажимает шаги картины: вошедший демо-сотрудник, иначе менеджер отеля."""

    manager: StaffActor
    by_login: dict[str, StaffActor]

    def of(self, *logins: str) -> StaffActor:
        """Первый вошедший из `logins`; никто не вошёл — менеджер."""
        for staff_login in logins:
            actor = self.by_login.get(staff_login)
            if actor is not None:
                return actor
        return self.manager

    def maid_for(self, room_number: str) -> StaffActor:
        """Горничная этажа номера, иначе старшая, иначе менеджер."""
        floor = housekeeping_api.floor_of_room(room_number)
        logins = [member.login for member in DEMO_STAFF if member.floor == floor]
        return self.of(*logins, SENIOR_LOGIN)


def _is_active(member: TenantMemberView) -> bool:
    return (
        member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
    )


def _actor(member: TenantMemberView) -> StaffActor:
    return StaffActor(user_id=member.user_id, display_name=member.display_name, login=member.login)


def invite_url(token: str) -> str:
    """Та же ссылка, что у страницы «Сотрудники» (`staff_portal/team.invite_url`):
    кабинет команде не импортировать (контракт 7 import-linter)."""
    return f"{get_settings().public_base_url.rstrip('/')}/staff/invite/{token}"


async def ensure_reception_category() -> bool:
    """Служба «Ресепшен» у демо-отеля; True — заведена сейчас. Контекст тенанта — у вызывающего."""
    try:
        await requests_api.create_category(RECEPTION_CATEGORY)
    except AppError as error:
        if error.code != requests_api.ERR_REQUESTS_CATEGORY_KEY_TAKEN:
            raise
        return False
    return True


async def ensure_staff(tenant_id: uuid.UUID) -> tuple[DemoActors, list[str]]:
    """Приглашения демо-сотрудникам, ещё не вошедшим; личности шагов; строки для печати."""
    members = await list_tenant_members(tenant_id)
    managers = [
        member
        for member in members
        if member.role_key is StaffRole.MANAGER
        and not member.is_platform_admin
        and _is_active(member)
    ]
    if not managers:
        raise DemoShowcaseError(
            "В демо-отеле нет активного менеджера — приглашать некому. Сначала "
            "python -m hospitality.tools.staff_bootstrap <ЛОГИН> --name <Имя> "
            "--tenant-slug demo-hotel."
        )
    manager = _actor(managers[0])
    accounts = {member.login: member for member in members if member.login is not None}
    pending = {invite.login: invite for invite in await list_pending_invites(tenant_id)}
    known_services = {category.key for category in await requests_api.list_categories()}
    by_login: dict[str, StaffActor] = {}
    lines: list[str] = []
    for demo in DEMO_STAFF:
        account = accounts.get(demo.login)
        if account is not None:
            if _is_active(account):
                by_login[demo.login] = _actor(account)
                lines.append(f"  {demo.name} ({demo.login}) — вход уже есть, ссылка не нужна.")
            else:
                lines.append(
                    f"  {demo.name} ({demo.login}) — учётка отключена, войти не выйдет: "
                    "включите её на странице «Сотрудники»."
                )
            continue
        stale = pending.get(demo.login)
        if stale is not None:
            await revoke_invite(stale.invite_id, tenant_id=tenant_id, actor_user_id=manager.user_id)
        grant = await create_invite(
            tenant_id,
            demo.role,
            demo.name,
            demo.login,
            invited_by=manager.user_id,
            service_keys=normalize_service_keys(
                demo.role, [demo.service], known_service_keys=known_services
            ),
        )
        lines.append(f"  {demo.name} — логин {grant.login}: {invite_url(grant.invite_token)}")
    return DemoActors(manager=manager, by_login=by_login), lines


async def assign_floors(actors: DemoActors) -> str:
    """Этажи вошедшим горничным (spec 0053); чужие строки раздачи остаются как были."""
    if not get_settings().enable_housekeeping_floors:
        return "Этажи: раздел выключен (ENABLE_HOUSEKEEPING_FLOORS), раздача пропущена."
    ours = {
        actor.user_id: frozenset({demo.floor})
        for demo in DEMO_STAFF
        if demo.floor is not None and (actor := actors.by_login.get(demo.login)) is not None
    }
    if not ours:
        return (
            "Этажи: горничные ещё не приняли приглашения — после их входа "
            "запустите команду ещё раз, она раздаст этажи 15 и 16."
        )
    kept: dict[uuid.UUID, set[int]] = {}
    for row in await housekeeping_api.list_floor_assignments():
        if row.user_id not in ours:
            kept.setdefault(row.user_id, set()).add(row.floor)
    await housekeeping_api.replace_floor_assignments(
        {**{user_id: frozenset(floors) for user_id, floors in kept.items()}, **ours}
    )
    return f"Этажи: раздано горничным — {len(ours)} из 2."
