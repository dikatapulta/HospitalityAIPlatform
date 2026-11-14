"""След нового менеджера отеля (#970): строка журнала изменений и уведомление основателю.

Уровень «Менеджер» — ключ от всего кабинета: «Сотрудники», приглашения, уровни
других. Без следа тот, кто сел за незапертую сессию менеджера на общем компьютере
стойки, выпускал себе приглашение этого уровня и принимал его на своём телефоне,
а среди сотен строк «Сотрудников» лишнего менеджера не замечали.

Новый менеджер рождается четырьмя дверями: приглашение по одному
(`staff_invites.create_invite`), списком (`staff_invite_batch.create_invites`),
«Новая ссылка» у приглашения этого уровня (`staff_invite_reissue.reissue_invite`)
и смена уровня в «Сотрудниках» (`staff_team.change_member_access`). Каждая зовёт
`record_manager_grant` в транзакции самого действия: не записалось действие — нет
ни строки, ни уведомления.

- Строка журнала (spec 0041) — по одной на приглашение, от того, кто действует:
  её видит менеджер отеля. Путь без личности (`actor` None — CLI, демо) строки не
  пишет, как весь журнал (spec 0041 §2.2).
- `SuperadminActed` — уведомление основателю тем же путём, что у сброса пароля
  менеджером (spec 0044 §6, #944): отель, время, роль и число, без логинов и имён.

Потолок ожидающих приглашений этого уровня — `staff_invites.ensure_manager_invite_room`,
гашение приглашений при отключении выпустившего — `staff_invites.revoke_issued_invites`.

Перед каждой из четырёх дверей кабинет спрашивает пароль того, кто действует
(`require_grant_password`, слово основателя 14.11 в #970): незапертая сессия сама по
себе нового менеджера уже не выпускает. Зовут HTTP-действия кабинета до выпуска;
путь без сессии (CLI, демо) пароля не спрашивает — человека у него нет.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.platform.events import SuperadminActed, SuperadminActionKind
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_actions import (
    StaffAction,
    StaffActionRecord,
    StaffActor,
    record_staff_action,
)
from hospitality.platform.staff_passkeys import confirmation_refused, require_password
from hospitality.shared.db import utc_now
from hospitality.shared.events import publish

if TYPE_CHECKING:
    from hospitality.platform.staff_auth import StaffContext

_JOURNAL_ACTIONS: dict[SuperadminActionKind, StaffAction] = {
    SuperadminActionKind.MANAGER_INVITE_ISSUED: StaffAction.MANAGER_INVITE_ISSUED,
    SuperadminActionKind.MEMBER_MADE_MANAGER: StaffAction.MEMBER_MADE_MANAGER,
}


async def record_manager_grant(
    session: AsyncSession,
    kind: SuperadminActionKind,
    *,
    actor_user_id: uuid.UUID,
    actor: StaffActor | None,
    count: int = 1,
    target_user_id: uuid.UUID | None = None,
    target_role: StaffRole | None = None,
) -> None:
    """Строки журнала и уведомление основателю — в сессии ДЕЙСТВИЯ.

    `kind` — `MANAGER_INVITE_ISSUED` (`count` — сколько приглашений этим действием)
    или `MEMBER_MADE_MANAGER` (`target_role` — уровень ДО смены). Требует
    `tenant_context`: журнал — тенантная таблица под RLS, событие берёт тенант из
    контекста (P-4)."""
    action = _JOURNAL_ACTIONS[kind]
    if actor is not None:
        for _ in range(count):
            record_staff_action(session, actor, StaffActionRecord(action=action))
    await publish(
        session,
        SuperadminActed(
            kind=kind,
            occurred_at=utc_now(),
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            target_role=target_role,
            count=count,
        ),
    )


async def require_grant_password(
    staff: StaffContext, password: str | None, *, client_ip: str, device_token: str | None
) -> None:
    """Пароль того, кто выпускает менеджера, — до выпуска (#970, слово основателя 14.11).

    Проверка — `staff_passkeys.require_password` под бюджетами входа: не подошёл —
    ERR-AUTH-017 (`password`), попытка списана; исчерпан бюджет — ERR-AUTH-006. Пароля
    в запросе нет — тот же ERR-AUTH-017, бюджет не тратится: пустая форма — не попытка."""
    if not password:
        raise confirmation_refused("password")
    await require_password(staff, password, client_ip=client_ip, device_token=device_token)
