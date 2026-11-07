"""Этажи хаускипинга в кабинете (spec 0053, issue #478): права, раздача и адресация.

Старшая хаускипинга раздаёт этажи, и заявка уборки из 704 зовёт горничную
7-го этажа — звук вкладки и пуш на телефон (`signal.build_signal`). С #856 этаж
решает и кто видит: горничная видит заявки уборки своих этажей, направленные ей
и без этажа (`FloorRouting.floor_scope` — срез ленты в SQL), старшая и менеджер —
все, и направляют заявку горничной (`direct_request`). Доска выездов остаётся
общей (§2). Раздачу хранит модуль `housekeeping`; здесь то, что знает только
кабинет: кто вправе раздавать и направлять (`rights_for` — одна функция на меню,
страницу и действия, P-12), какие строки раздачи действуют (активные сотрудники
уборки) и кого зовёт и кому видна заявка (`FloorRouting`). Что рисует страница —
`floors_view.py`.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from hospitality.modules.housekeeping import api as housekeeping_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_team
from hospitality.platform.config import TenantConfig
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
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


def floors_narrow(config: TenantConfig | None) -> bool:
    """Сужают ли этажи круг вообще (spec 0053 §4): нужен срок уборки отеля.

    Без срока ступеней по времени нет, и забытая раздача оставила бы заявку без
    слушателей и без глаз навсегда (ревью PR #552, #606) — тогда этаж не сужает
    ни сигнал, ни ленту. Одна проверка на сигнал, пуш и очередь (P-12).
    """
    return config is not None and config.reminder_delay_for(HOUSEKEEPING_SERVICE_KEY) is not None


@dataclass(frozen=True)
class FloorRights:
    """Что сотрудник вправе на странице «Этажи» (spec 0053 §5, docs/RBAC.md §2)."""

    can_view: bool
    can_edit: bool


def rights_for(staff: StaffAudience) -> FloorRights:
    """Смотрят хаускипинг, ресепшен и менеджер; раздают старший уборки и менеджер —
    они же направляют заявку уборки горничной (spec 0053 §4а)."""
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


def floors_line(user_floors: frozenset[int]) -> str:
    """Подпись горничной в списке доски и в «Направить» — та же, что пересчитывает
    `floors.js`: «этаж 7», «этажи 7, 8, 12-14», «без этажа»."""
    if not user_floors:
        return "без этажа"
    if len(user_floors) == 1:
        return f"этаж {next(iter(user_floors))}"
    return f"этажи {format_floors(user_floors)}"


@dataclass(frozen=True)
class FloorRouting:
    """Раздача глазами одного сотрудника уборки: этажи с командой, его этажи и он сам.

    Значение хэшируемое: рассылка пуша группирует по нему вместе с уровнем и
    службами — у группы один ответ сигнала. Пустое — этажей нет, как раньше.
    `own_floors_only` — горничная (уровень «сотрудник», не ресепшен): ей видна
    и звучит заявка уборки по этажу только своих этажей или направленная ей
    (§2, #856); старшая видит все, а звучат ей с первой ступени только свои.
    """

    staffed_floors: frozenset[int] = frozenset()
    my_floors: frozenset[int] = frozenset()
    user_id: uuid.UUID | None = None
    own_floors_only: bool = False

    def is_floor_call(self, room_number: str | None) -> bool:
        """Заявка по этажу: у номера есть этаж, у этажа — команда."""
        floor = housekeeping_api.floor_of_room(room_number)
        return floor is not None and floor in self.staffed_floors

    def calls_me_first(
        self, room_number: str | None, *, directed_to: uuid.UUID | None = None
    ) -> bool:
        """Зовёт ли меня заявка уборки этого номера на первой ступени: направленная
        мне — да; заявка по этажу — только команду этажа; иначе — всю службу."""
        if directed_to is not None and directed_to == self.user_id:
            return True
        if not self.is_floor_call(room_number):
            return True
        return housekeeping_api.floor_of_room(room_number) in self.my_floors

    def floor_scope(
        self, categories: Iterable[requests_api.RequestCategoryRead]
    ) -> requests_api.FloorScope | None:
        """Срез ленты горничной (§2): заявки уборки с этажей других команд не видны,
        кроме взятых ею и направленных ей, срочных и эскалаций. None — не срезать:
        не горничная, раздачи нет или все этажи с командой — её."""
        hidden = self.staffed_floors - self.my_floors
        if not self.own_floors_only or self.user_id is None or not hidden:
            return None
        ids = frozenset(c.id for c in categories if c.key == HOUSEKEEPING_SERVICE_KEY)
        if not ids:
            return None
        return requests_api.FloorScope(category_ids=ids, hidden_floors=hidden, user_id=self.user_id)


NO_ROUTING: Final = FloorRouting()


@dataclass(frozen=True)
class TenantFloors:
    """Действующая раздача отеля: строки активных сотрудников уборки."""

    by_user: Mapping[uuid.UUID, frozenset[int]] = field(default_factory=dict)

    @property
    def staffed_floors(self) -> frozenset[int]:
        return frozenset(floor for floors in self.by_user.values() for floor in floors)

    def routing_for(self, staff: StaffAudience, user_id: uuid.UUID) -> FloorRouting:
        """Раздача глазами сотрудника — одна на вкладку, пуш и ленту (P-12). Не
        хаускипинг — пусто: заявка уборки его не зовёт и не прячется от него."""
        if not self.by_user or not staff.is_housekeeping:
            return NO_ROUTING
        return FloorRouting(
            staffed_floors=self.staffed_floors,
            my_floors=self.by_user.get(user_id, frozenset()),
            user_id=user_id,
            own_floors_only=staff.role_key is StaffRole.STAFF and not staff.sees_all_services,
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


async def floor_scope_for(
    staff: StaffContext,
    config: TenantConfig | None,
    categories: Iterable[requests_api.RequestCategoryRead],
) -> requests_api.FloorScope | None:
    """Срез ленты по этажам для очереди, главной и числа в меню (§2, #856).

    Раздача читается только у горничной и только когда этажи сужают
    (`floors_narrow`): старшей, ресепшену и менеджеру срез не нужен.
    """
    if not (staff.is_housekeeping and staff.role_key is StaffRole.STAFF) or not floors_narrow(
        config
    ):
        return None
    return (await routing_for_staff(staff)).floor_scope(categories)


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


async def housekeeping_members(tenant_id: uuid.UUID) -> list[staff_team.TenantMemberView]:
    """Активные сотрудники уборки отеля по имени — доска «Этажи» и список «Направить»."""
    return [
        member
        for member in await staff_team.list_tenant_members(tenant_id)
        if member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
        and HOUSEKEEPING_SERVICE_KEY in member.service_keys
    ]


async def direct_request(
    staff: StaffContext, request_id: uuid.UUID, to_user_id: uuid.UUID
) -> requests_api.ServiceRequestRead:
    """«Направить» невзятую заявку уборки горничной (spec 0053 §4а, #856).

    Направляют те же, кто раздаёт этажи (`require_edit_right`, иначе 403
    ERR-HOUSEKEEPING-003); адресат — активный сотрудник уборки этого отеля, иначе
    409 ERR-HOUSEKEEPING-006 (состав сменился, пока панель была открыта); заявка —
    только службы уборки (домен: ERR-REQUESTS-007), только невзятая (ERR-REQUESTS-003).
    """
    require_edit_right(staff)
    target = next(
        (m for m in await housekeeping_members(staff.tenant_id) if m.user_id == to_user_id), None
    )
    if target is None:
        raise not_floor_member()
    housekeeping_ids = frozenset(
        category.id
        for category in await requests_api.list_categories()
        if category.key == HOUSEKEEPING_SERVICE_KEY
    )
    updated = await requests_api.direct_request(
        request_id,
        to_user_id=target.user_id,
        to_display_name=target.display_name,
        acting_user=staff.actor(),
        allowed_category_ids=housekeeping_ids,
    )
    logger.info(
        "staff.request_directed",
        request_id=str(request_id),
        user_id=str(staff.user_id),
        to_user_id=str(target.user_id),
    )
    return updated


async def direct_choices(
    staff: StaffContext,
    requests: Sequence[requests_api.ServiceRequestRead],
    category_keys: Mapping[uuid.UUID, str],
) -> list[dict[str, str]] | None:
    """Кому можно направить заявку уборки (§4а): «Айгуль — этажи 7, 8» по имени.

    None — раздел выключен, не по праву или в ленте нет невзятой заявки уборки:
    тогда состав службы и раздача не читаются.
    """
    if not section_enabled() or not rights_for(staff).can_edit:
        return None
    if not any(
        request.status is requests_api.RequestStatus.NEW
        and category_keys.get(request.category_id) == HOUSEKEEPING_SERVICE_KEY
        for request in requests
    ):
        return None
    tenant_floors = await load_tenant_floors(staff.tenant_id)
    return [
        {
            "id": str(member.user_id),
            "label": f"{member.display_name} — "
            + floors_line(tenant_floors.by_user.get(member.user_id, frozenset())),
        }
        for member in await housekeeping_members(staff.tenant_id)
    ]


def floors_path(tenant_slug: str) -> str:
    return f"/staff/{tenant_slug}/floors"
