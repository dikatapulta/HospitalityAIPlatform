"""Этажи хаускипинга в кабинете (spec 0053, issue #478): права, раздача и адресация.

Старшая хаускипинга раздаёт этажи, и заявка уборки из 704 сначала зовёт
горничную 7-го этажа — звук вкладки и пуш на телефон (`signal.build_signal`).
Этаж решает, кого звать, а не кто видит: очередь и доска выездов показывают
службе всё, как раньше (§2). Раздачу хранит модуль `housekeeping`; здесь то, что
знает только кабинет: кто вправе раздавать (`rights_for` — одна функция на меню,
страницу и действие, P-12), какие строки раздачи действуют (активные сотрудники
уборки) и кого зовёт заявка на первой ступени лестницы (`FloorRouting`). Что рисует
страница — `floors_view.py`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.platform import staff_team
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import StaffAudience, StaffContext
from hospitality.platform.staff_services import HOUSEKEEPING_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import turnovers

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8); не по праву — 003 доски.
ERR_HOUSEKEEPING_NOT_FLOOR_MEMBER: Final = "ERR-HOUSEKEEPING-006"

# Поле «Этажи» одной горничной: «7, 8, 12-14» с запасом.
FLOORS_TEXT_MAX_LENGTH: Final = 200

_DASHES: Final = re.compile(r"\s*[-–—]\s*")
_SEPARATORS: Final = re.compile(r"[,;\s]+")
_NUMBER: Final = re.compile(r"[0-9]{1,2}")


def section_enabled() -> bool:
    """`ENABLE_HOUSEKEEPING_FLOORS` (spec 0053 §7): одна проверка на меню, страницу,
    действие и сигнал — выключено, и этажей нет совсем, сигнал зовёт как раньше."""
    return get_settings().enable_housekeeping_floors


@dataclass(frozen=True)
class FloorRights:
    """Что сотрудник вправе на странице «Этажи» (spec 0053 §5, docs/RBAC.md §2)."""

    can_view: bool
    can_edit: bool


def rights_for(staff: StaffAudience) -> FloorRights:
    """Смотрят хаускипинг, ресепшен и менеджер; раздают старший уборки и менеджер."""
    is_manager = staff.role_key is StaffRole.MANAGER
    housekeeping = staff.is_housekeeping
    return FloorRights(
        can_view=is_manager or staff.is_reception or housekeeping,
        can_edit=is_manager or (housekeeping and staff.role_key is StaffRole.SENIOR),
    )


def require_edit_right(staff: StaffContext) -> None:
    """Раздаёт не старший уборки и не менеджер — ERR-HOUSEKEEPING-003 (403)."""
    if not rights_for(staff).can_edit:
        raise AppError(
            code=turnovers.ERR_HOUSEKEEPING_STEP_FORBIDDEN,
            message="Floor distribution is not available to you",
            status_code=403,
        )


def not_floor_member() -> AppError:
    """В раздаче сотрудник, который уже не в службе уборки этого отеля (409)."""
    return AppError(
        code=ERR_HOUSEKEEPING_NOT_FLOOR_MEMBER,
        message="Floor assignment names someone outside this hotel's housekeeping",
        status_code=409,
    )


def parse_floors(text: str) -> frozenset[int]:
    """«7, 8, 12-14» → {7, 8, 12, 13, 14}; пусто — без этажа.

    Разделители — запятая, точка с запятой, пробел; диапазон — дефис или тире,
    перевёрнутый «14-12» тоже понятен. Иное или этаж вне 1–99 — ValueError.
    """
    floors: set[int] = set()
    normalized = _DASHES.sub("-", text.strip())
    for token in filter(None, _SEPARATORS.split(normalized)):
        bounds = token.split("-")
        if len(bounds) > 2 or not all(_NUMBER.fullmatch(bound) for bound in bounds):
            raise ValueError(f"floors not understood: {token!r}")
        numbers = sorted(int(bound) for bound in bounds)
        first, last = numbers[0], numbers[-1]
        if first < housekeeping_api.FLOOR_MIN or last > housekeeping_api.FLOOR_MAX:
            raise ValueError("floor out of range")
        floors.update(range(first, last + 1))
    return frozenset(floors)


def format_floors(floors: frozenset[int]) -> str:
    """{7, 8, 12, 13, 14} → «7, 8, 12-14»: подряд от трёх — диапазоном."""
    runs: list[list[int]] = []
    for floor in sorted(floors):
        if runs and floor == runs[-1][-1] + 1:
            runs[-1].append(floor)
        else:
            runs.append([floor])
    return ", ".join(
        f"{run[0]}-{run[-1]}" if len(run) >= 3 else ", ".join(map(str, run)) for run in runs
    )


@dataclass(frozen=True)
class FloorRouting:
    """Раздача глазами одного сотрудника: этажи с командой и его этажи (spec 0053 §4).

    Значение хэшируемое: рассылка пуша группирует по нему вместе с уровнем и
    службами — у группы один ответ сигнала. Пустое — этажей нет, как раньше.
    """

    staffed_floors: frozenset[int] = frozenset()
    my_floors: frozenset[int] = frozenset()

    def is_floor_call(self, room_number: str | None) -> bool:
        """Заявка по этажу: у номера есть этаж, у этажа — команда."""
        floor = housekeeping_api.floor_of_room(room_number)
        return floor is not None and floor in self.staffed_floors

    def calls_me_first(self, room_number: str | None) -> bool:
        """Зовёт ли меня заявка уборки этого номера на первой ступени: заявка по
        этажу — только команду этажа, иначе — всю службу."""
        if not self.is_floor_call(room_number):
            return True
        return housekeeping_api.floor_of_room(room_number) in self.my_floors


NO_ROUTING: Final = FloorRouting()


@dataclass(frozen=True)
class TenantFloors:
    """Действующая раздача отеля: строки активных сотрудников уборки."""

    by_user: Mapping[uuid.UUID, frozenset[int]] = field(default_factory=dict)

    @property
    def staffed_floors(self) -> frozenset[int]:
        return frozenset(floor for floors in self.by_user.values() for floor in floors)

    def routing_for(self, staff: StaffAudience, user_id: uuid.UUID) -> FloorRouting:
        """Раздача глазами сотрудника — одна на вкладку и пуш (P-12). Не хаускипинг —
        пусто: первая ступень заявки уборки его и так не зовёт."""
        if not self.by_user or not staff.is_housekeeping:
            return NO_ROUTING
        return FloorRouting(
            staffed_floors=self.staffed_floors,
            my_floors=self.by_user.get(user_id, frozenset()),
        )


async def load_tenant_floors(tenant_id: uuid.UUID) -> TenantFloors:
    """Раздача отеля без строк отключённых, выведенных из уборки и стёртых (§4).

    Раздел выключен — пусто без запроса в базу (§7). RLS — контекст тенанта.
    """
    if not section_enabled():
        return TenantFloors()
    assignments = await housekeeping_api.list_floor_assignments()
    if not assignments:
        return TenantFloors()
    active = await staff_team.active_service_member_ids(tenant_id, HOUSEKEEPING_SERVICE_KEY)
    by_user: dict[uuid.UUID, set[int]] = {}
    for assignment in assignments:
        if assignment.user_id in active:
            by_user.setdefault(assignment.user_id, set()).add(assignment.floor)
    return TenantFloors(by_user={user: frozenset(floors) for user, floors in by_user.items()})


async def routing_for_staff(staff: StaffContext) -> FloorRouting:
    """Раздача для сигнала вкладки: читается только у хаускипинга — остальных
    первая ступень заявки уборки и так не зовёт."""
    if not section_enabled() or not staff.is_housekeeping:
        return NO_ROUTING
    return (await load_tenant_floors(staff.tenant_id)).routing_for(staff, staff.user_id)


async def save_floors(
    staff: StaffContext, floors_by_user: Mapping[uuid.UUID, frozenset[int]]
) -> None:
    """«Сохранить раздачу»: только старший уборки и менеджер; в раздаче — только
    активные сотрудники уборки этого отеля, иначе 409 ERR-HOUSEKEEPING-006 (состав
    сменился, пока страница была открыта)."""
    require_edit_right(staff)
    active = await staff_team.active_service_member_ids(staff.tenant_id, HOUSEKEEPING_SERVICE_KEY)
    if not set(floors_by_user) <= active:
        raise not_floor_member()
    rows = await housekeeping_api.replace_floor_assignments(floors_by_user)
    logger.info(
        "staff.floors_saved", user_id=str(staff.user_id), members=len(floors_by_user), rows=rows
    )


def floors_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/floors"
