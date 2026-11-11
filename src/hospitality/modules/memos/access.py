"""Кто видит запись и кто может в ней участвовать (spec 0077 §5, §6, §8) и коды ошибок.

Видят запись только участники: черновик — автор; отправленную — автор, подписанты
и адресат. «Отправленная» — по `sent_at`, а не по статусу: черновик, отозванный
до отправки, тоже видит только автор (ревью PR #946). Реестра «весь отель» нет
ни у кого, у суперадмина и менеджера тоже: чужая запись отвечает ERR-MEMO-001, как
несуществующая. Изоляцию отелей держит RLS (P-4).

Требование объяснения видят двое — затребовавший и сотрудник; у объяснительной по
требованию адресат — затребовавший, поэтому её видят автор и он. Требовать может
`manager` — у любого сотрудника отеля, `senior` — у `staff` и `senior` своих служб (§6, §8).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.memos.models import (
    Memo,
    MemoDemand,
    MemoSignature,
    MemoStatus,
    SignatureRole,
)
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
from hospitality.platform.staff_auth import ERR_AUTH_FORBIDDEN
from hospitality.platform.staff_team import TenantMemberView, list_tenant_members
from hospitality.shared.errors import VALIDATION_ERROR_CODE, AppError

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

ERR_MEMO_NOT_FOUND: Final = "ERR-MEMO-001"
ERR_MEMO_WRONG_STATE: Final = "ERR-MEMO-002"
ERR_MEMO_NO_SIGNATURE_SAMPLE: Final = "ERR-MEMO-003"

ADDRESSEE_ROLES: Final = frozenset({StaffRole.SENIOR, StaffRole.MANAGER})


def not_found() -> AppError:
    return AppError(code=ERR_MEMO_NOT_FOUND, message="Запись не найдена.", status_code=404)


def wrong_state() -> AppError:
    # Повтор из второй вкладки или действие не по статусу (§3).
    return AppError(
        code=ERR_MEMO_WRONG_STATE,
        message="Запись уже изменилась — обновите страницу.",
        status_code=409,
    )


def no_signature_sample() -> AppError:
    return AppError(
        code=ERR_MEMO_NO_SIGNATURE_SAMPLE,
        message="Сначала настройте подпись в разделе «Моя подпись».",
        status_code=409,
    )


def demand_forbidden() -> AppError:
    # Кнопки «Затребовать объяснение» у `staff` и суперадмина нет; кабинет не пустит раньше.
    return AppError(
        code=ERR_AUTH_FORBIDDEN,
        message="Требовать объяснение может только руководитель.",
        status_code=403,
    )


def invalid_input(field: str, reason: str) -> AppError:
    return AppError(
        code=VALIDATION_ERROR_CODE,
        message="Форма заполнена неверно.",
        status_code=422,
        details=[{"field": field, "reason": reason}],
    )


async def signature_rows(session: AsyncSession, memo_id: uuid.UUID) -> list[MemoSignature]:
    rows = await session.scalars(
        select(MemoSignature)
        .where(MemoSignature.memo_id == memo_id)
        .order_by(MemoSignature.created_at, MemoSignature.id)
    )
    return list(rows)


def signer_ids(rows: Collection[MemoSignature]) -> list[uuid.UUID]:
    return [row.user_id for row in rows if row.role is SignatureRole.SIGNER]


def can_see(memo: Memo, rows: Collection[MemoSignature], user_id: uuid.UUID) -> bool:
    if memo.author_id == user_id:
        return True
    if memo.sent_at is None:
        return False
    return memo.addressee_id == user_id or any(row.user_id == user_id for row in rows)


async def load_visible(
    session: AsyncSession, staff: StaffContext, memo_id: uuid.UUID, *, lock: bool = False
) -> tuple[Memo, list[MemoSignature]]:
    """Запись и её лист — только участнику; иначе ERR-MEMO-001."""
    query = select(Memo).where(Memo.id == memo_id)
    if lock:
        query = query.with_for_update()
    memo = await session.scalar(query)
    if memo is None:
        raise not_found()
    rows = await signature_rows(session, memo.id)
    if not can_see(memo, rows, staff.user_id):
        raise not_found()
    return memo, rows


def require_author(memo: Memo, staff: StaffContext, *statuses: MemoStatus) -> None:
    # Действие автора не от автора — как чужая запись: ему этой кнопки нет.
    if memo.author_id != staff.user_id:
        raise not_found()
    if memo.status not in statuses:
        raise wrong_state()


async def active_members(tenant_id: uuid.UUID) -> dict[uuid.UUID, TenantMemberView]:
    """Действующие сотрудники отеля по id; суперадмина среди них нет."""
    return {
        member.user_id: member
        for member in await list_tenant_members(tenant_id)
        if member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
        and not member.is_platform_admin
    }


async def check_participants(
    staff: StaffContext, addressee_id: uuid.UUID, signers: Collection[uuid.UUID]
) -> None:
    """Адресат — действующий `senior` или `manager`; подписанты — действующие сотрудники
    отеля; ни тот, ни другие — не автор, адресат — не подписант (§3.1)."""
    active = await active_members(staff.tenant_id)
    addressee = active.get(addressee_id)
    if addressee is None or addressee_id == staff.user_id:
        raise invalid_input("addressee_id", "not_a_member")
    if addressee.role_key not in ADDRESSEE_ROLES:
        raise invalid_input("addressee_id", "not_senior_or_manager")
    for signer_id in signers:
        if signer_id not in active or signer_id in (staff.user_id, addressee_id):
            raise invalid_input("signer_ids", "not_a_member")


async def check_explanation_addressee(staff: StaffContext, memo: Memo) -> None:
    """Адресат объяснительной перед подачей. По требованию — затребовавший: он мог
    с тех пор сменить уровень, но остаться в отеле обязан; иначе — как у записки."""
    if memo.demand_id is None:
        await check_participants(staff, memo.addressee_id, ())
        return
    if memo.addressee_id not in await active_members(staff.tenant_id):
        raise invalid_input("addressee_id", "not_a_member")


async def check_demand_target(staff: StaffContext, employee_id: uuid.UUID) -> None:
    """Может ли вошедший требовать объяснение у этого сотрудника (§6, §8)."""
    if staff.is_platform_admin or staff.role_key not in ADDRESSEE_ROLES:
        raise demand_forbidden()
    target = (await active_members(staff.tenant_id)).get(employee_id)
    if target is None or employee_id == staff.user_id:
        raise invalid_input("employee_id", "not_a_member")
    if staff.role_key is StaffRole.SENIOR and (
        target.role_key is StaffRole.MANAGER or not staff.service_keys & set(target.service_keys)
    ):
        raise invalid_input("employee_id", "not_your_service")


def can_see_demand(demand: MemoDemand, user_id: uuid.UUID) -> bool:
    return user_id in (demand.demanded_by_id, demand.employee_id)


async def load_demand(
    session: AsyncSession, staff: StaffContext, demand_id: uuid.UUID, *, lock: bool = False
) -> MemoDemand:
    """Требование — только затребовавшему и сотруднику; иначе ERR-MEMO-001."""
    query = select(MemoDemand).where(MemoDemand.id == demand_id)
    if lock:
        query = query.with_for_update()
    demand = await session.scalar(query)
    if demand is None or not can_see_demand(demand, staff.user_id):
        raise not_found()
    return demand
