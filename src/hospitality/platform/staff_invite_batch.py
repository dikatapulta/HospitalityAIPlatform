"""Приглашения списком (spec 0072 §4): весь список — или ни одного.

Менеджер вставляет столбец имён, страница подсказывает логины, а сервер проверяет
весь список до записи: формат и свободу каждого логина (правило занятости —
`staff_invites.taken_logins`, spec 0037 §4) и повтор внутри списка. Хоть одна
плохая строка — не выпускается ни одно приглашение: ERR-AUTH-012 (422), в
`details` — номер строки и причина, страница пишет свой текст у строки. Нет —
все приглашения одной транзакцией.

Права, срок ссылки и принятие — как у одиночного приглашения
(`staff_invites.create_invite`): строка та же (`staff_invites.new_invite`),
принимает её та же страница, новой двери нет. Гонку двух менеджеров с одним
логином, как и там, ловит UNIQUE идентичности при принятии (ERR-AUTH-004).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel

from hospitality.platform import staff_invites
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_credentials import ERR_AUTH_LOGIN_INVALID, normalize_login
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Строк за раз (spec 0072 §4): лист печати на сотню карточек — уже пачка бумаги,
# а одна транзакция на сотню строк — доли секунды.
MAX_BATCH_INVITES: Final = 100

# Причина у строки — страница выбирает по ней свой текст (spec 0072 §8):
# не тот формат, тот же логин выше по списку, занят учёткой или приглашением.
RowProblem = Literal["format", "duplicate", "taken"]


class InviteRow(BaseModel):
    """Строка списка: имя (его увидит сам сотрудник) и логин, как прислала страница."""

    invited_name: str
    login: str


def _problems(logins: Sequence[str | None], taken: set[str]) -> list[dict[str, Any]]:
    """Плохие строки списка: номер с 1, как у менеджера на экране, и причина.

    Повтор отмечается у второй и следующих строк: первая получает свою судьбу
    (свободен или занят), а «уже есть в списке выше» — подсказка, какую править."""
    problems: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row, login in enumerate(logins, start=1):
        reason: RowProblem | None = None
        if login is None:
            reason = "format"
        elif login in seen:
            reason = "duplicate"
        elif login in taken:
            reason = "taken"
        if login is not None:
            seen.add(login)
        if reason is not None:
            problems.append({"row": row, "reason": reason})
    return problems


async def create_invites(
    tenant_id: uuid.UUID,
    role_key: StaffRole,
    rows: Sequence[InviteRow],
    *,
    invited_by: uuid.UUID,
    service_keys: Sequence[str] = (),
) -> list[staff_invites.StaffInviteGrant]:
    """Выпустить приглашения списком: одни уровень и службы на всех (spec 0072 §4).

    `service_keys` — уже приведённые `staff_services.normalize_service_keys`, как
    у `create_invite`. Размер списка (1…`MAX_BATCH_INVITES`) держит схема запроса;
    здесь он проверяется ещё раз — иначе вызов из кода выпустил бы что угодно.
    Гранты — в порядке строк; токены в них — единственный раз, в БД только хэши.
    """
    if not 1 <= len(rows) <= MAX_BATCH_INVITES:
        raise ValueError(f"batch must have 1..{MAX_BATCH_INVITES} rows, got {len(rows)}")
    logins = [normalize_login(row.login) for row in rows]
    async with platform_session_scope() as session:
        taken = await staff_invites.taken_logins(
            session, tenant_id, {login for login in logins if login is not None}
        )
        problems = _problems(logins, taken)
        if problems:
            raise AppError(
                code=ERR_AUTH_LOGIN_INVALID,
                message="Some logins in the list are malformed, repeated or already taken",
                status_code=422,
                details=problems,
            )
        # Без плохих строк у каждой строки есть логин: `strict` — страховка порядка.
        checked = [login for login in logins if login is not None]
        issued = [
            staff_invites.new_invite(
                tenant_id,
                role_key,
                row.invited_name,
                login,
                invited_by=invited_by,
                service_keys=service_keys,
            )
            for row, login in zip(rows, checked, strict=True)
        ]
        session.add_all(invite for invite, _ in issued)
        await session.flush()
    # Ни имён, ни логинов, ни токенов: id и число — по ним журнал сверяется с базой.
    logger.info(
        "staff.invite_batch_created",
        tenant_id=str(tenant_id),
        invite_ids=[str(invite.id) for invite, _ in issued],
        invite_count=len(issued),
        role_key=role_key.value,
        service_keys=list(service_keys),
        invited_by=str(invited_by),
    )
    return [
        staff_invites.StaffInviteGrant(
            invite_id=invite.id, invite_token=token, login=login, expires_at=invite.expires_at
        )
        for (invite, token), login in zip(issued, checked, strict=True)
    ]
