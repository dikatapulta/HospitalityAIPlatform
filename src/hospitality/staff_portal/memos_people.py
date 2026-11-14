"""Сотрудники отеля для экранов «Служебных записок» (spec 0077 §3.1, §6, §8; #936).

Имя и службы участника — всем уровням (§8): лист подписей, реестр, выбор адресата,
подписантов и сотрудника, у которого требуют объяснение. Суперадмина в списке нет.
Кого предлагать — подсказка формы: право проверяет модуль `memos` ещё раз
(`access.check_participants`, `access.check_demand_target`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_team
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal.team import services_label

ADDRESSEE_ROLES: Final = (StaffRole.SENIOR, StaffRole.MANAGER)
# Ушедший из отеля участник остаётся в листе; имени нет — учётку стёрли (spec 0044 §5).
GONE_NAME: Final = "Бывший сотрудник"


@dataclass(frozen=True)
class Person:
    name: str
    services: str
    role: StaffRole
    active: bool
    service_keys: frozenset[str] = frozenset()


async def directory(staff: StaffContext) -> dict[uuid.UUID, Person]:
    """Сотрудники отеля по id: имя и службы — всем уровням (§8). Суперадмина нет."""
    members = await staff_team.list_tenant_members(staff.tenant_id)
    names = {category.key: category.name for category in await requests_api.list_categories()}
    return {
        member.user_id: Person(
            name=member.display_name,
            services=services_label(member.service_keys, names),
            role=member.role_key,
            active=member.membership_status is MembershipStatus.ACTIVE
            and member.user_status is UserStatus.ACTIVE,
            service_keys=frozenset(member.service_keys),
        )
        for member in members
        if not member.is_platform_admin
    }


def person_name(people: dict[uuid.UUID, Person], user_id: uuid.UUID) -> str:
    person = people.get(user_id)
    return person.name if person else GONE_NAME


def others(staff: StaffContext, people: dict[uuid.UUID, Person]) -> list[tuple[uuid.UUID, Person]]:
    """Действующие сотрудники отеля, кроме вошедшего, по имени (выбор в формах)."""
    return sorted(
        (
            (user_id, person)
            for user_id, person in people.items()
            if person.active and user_id != staff.user_id
        ),
        key=lambda item: item[1].name.casefold(),
    )


def option(user_id: uuid.UUID, person: Person) -> dict[str, str]:
    return {"id": str(user_id), "name": person.name, "services": person.services}


def addressees(staff: StaffContext, people: dict[uuid.UUID, Person]) -> list[dict[str, str]]:
    """Кому можно адресовать записку или объяснительную: `senior` и `manager` (§3.1, §6)."""
    return [
        option(user_id, person)
        for user_id, person in others(staff, people)
        if person.role in ADDRESSEE_ROLES
    ]


def can_demand(staff: StaffContext) -> bool:
    """Требовать объяснение — `senior` и `manager`, не суперадмин (§6, §8)."""
    return not staff.is_platform_admin and staff.role_key in ADDRESSEE_ROLES


def demand_targets(staff: StaffContext, people: dict[uuid.UUID, Person]) -> list[dict[str, str]]:
    """У кого вошедший может требовать объяснение: `manager` — у любого, `senior` — у
    `staff` и `senior` своих служб (то же условие, что `access.check_demand_target`)."""
    if not can_demand(staff):
        return []
    return [
        option(user_id, person)
        for user_id, person in others(staff, people)
        if staff.role_key is StaffRole.MANAGER
        or (person.role is not StaffRole.MANAGER and staff.service_keys & person.service_keys)
    ]
