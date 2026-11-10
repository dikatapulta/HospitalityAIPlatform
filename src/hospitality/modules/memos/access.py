"""Кто видит запись и кто может в ней участвовать (spec 0077 §5, §8) и коды ошибок.

Видят запись только участники: черновик — автор; отправленную — автор, подписанты
и адресат. «Отправленная» — по `sent_at`, а не по статусу: черновик, отозванный
до отправки, тоже видит только автор (ревью PR #946). Реестра «весь отель» нет
ни у кого, у суперадмина и менеджера тоже: чужая запись отвечает ERR-MEMO-001, как
несуществующая. Изоляцию отелей держит RLS (P-4).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import TYPE_CHECKING, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.memos.models import Memo, MemoSignature, MemoStatus, SignatureRole
from hospitality.platform.models import MembershipStatus, StaffRole, UserStatus
from hospitality.platform.staff_team import list_tenant_members
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


async def check_participants(
    staff: StaffContext, addressee_id: uuid.UUID, signers: Collection[uuid.UUID]
) -> None:
    """Адресат — действующий `senior` или `manager`; подписанты — действующие сотрудники
    отеля; ни тот, ни другие — не автор, адресат — не подписант (§3.1)."""
    active = {
        member.user_id: member
        for member in await list_tenant_members(staff.tenant_id)
        if member.membership_status is MembershipStatus.ACTIVE
        and member.user_status is UserStatus.ACTIVE
        and not member.is_platform_admin
    }
    addressee = active.get(addressee_id)
    if addressee is None or addressee_id == staff.user_id:
        raise invalid_input("addressee_id", "not_a_member")
    if addressee.role_key not in ADDRESSEE_ROLES:
        raise invalid_input("addressee_id", "not_senior_or_manager")
    for signer_id in signers:
        if signer_id not in active or signer_id in (staff.user_id, addressee_id):
            raise invalid_input("signer_ids", "not_a_member")
