"""Журнал изменений кабинета: кто из сотрудников что сделал (spec 0041).

Строку пишет доменный модуль (`requests`, `guests`) той же сессией, что само
действие (`record_staff_action`), — откатилось действие, откатилась и строка.
Пишется только действие с личностью (`StaffActor`): путь без личности
(Telegram-суррогат, отмена гостем, заявка от ИИ, HTTP API) ничего не пишет,
строка «кто-то» на вопрос «кто из сотрудников» не отвечает (§2.2).
Исключение — автовыезд по сроку (spec 0043 §2.3, `record_automatic_action`):
его делает система, и без строки менеджер не узнал бы, кто выписал комнату.

Читают страница кабинета (`list_staff_actions`, `list_staff_action_logins`)
и ретеншн (`delete_staff_actions_before`, срок — как у текстов гостей,
spec 0032). Все функции работают в контексте тенанта под RLS (P-4).

Текстов гостя, примечаний и имён в журнале нет по построению — у
`StaffActionRecord` просто нет таких полей. Логин — PII: в логи не пишется.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

# StaffAction живёт рядом с моделью (тип её колонки), а контракт журнала —
# здесь (spec 0041 §3): явный реэкспорт, модули импортируют его отсюда.
from hospitality.platform.models import StaffAction as StaffAction
from hospitality.platform.models import StaffActionLog
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


class StaffActor(BaseModel):
    """Кто действует из кабинета (spec 0041 §2.3; в `requests` — `ActingUser`).

    Снапшот на момент действия: доменный модуль в платформенные таблицы не
    ходит, личность передаёт вызывающая сторона (`StaffContext.actor()`).
    `display_name` уходит в `claimed_by_*`/`closed_by_*` заявки (spec 0035),
    в журнал — только `login`. None вместо этой схемы — путь без личности.
    """

    user_id: uuid.UUID
    display_name: str = Field(min_length=1, max_length=255)
    login: str | None = Field(default=None, max_length=12)


class StaffActionRecord(BaseModel):
    """Что и над чем — поля строки журнала без `id`/`tenant_id`/`occurred_at`
    и без личности (её несёт `StaffActor`). Снапшоты, а не ссылки (§2.1)."""

    action: StaffAction
    request_id: uuid.UUID | None = None
    stay_id: uuid.UUID | None = None
    request_daily_number: int | None = None
    request_service_day: date | None = None
    category_name: str | None = Field(default=None, max_length=255)
    category_name_to: str | None = Field(default=None, max_length=255)
    room_number: str | None = Field(default=None, max_length=20)
    room_number_to: str | None = Field(default=None, max_length=20)
    nights: int | None = Field(default=None, ge=1, le=32767)
    with_note: bool = False


class StaffActionRead(StaffActionRecord):
    """Строка журнала для страницы кабинета."""

    model_config = ConfigDict(from_attributes=True)

    occurred_at: datetime
    actor_login: str | None


def record_staff_action(
    session: AsyncSession, actor: StaffActor, record: StaffActionRecord
) -> None:
    """Добавить строку журнала в сессию ДЕЙСТВИЯ — только `session.add`.

    Своей транзакции не открывает: строка коммитится и откатывается вместе с
    действием (§2.1). `occurred_at` ставит default модели при flush — тот же
    `utc_now()` той же транзакции.
    """
    session.add(
        StaffActionLog(actor_user_id=actor.user_id, actor_login=actor.login, **record.model_dump())
    )


def record_automatic_action(session: AsyncSession, record: StaffActionRecord) -> None:
    """Строка журнала о действии системы без личности (spec 0043 §2.3): `actor_*` = NULL.

    Та же сессия, что действие, — как у `record_staff_action`. Отличить такую
    строку от «учётки без логина» страница может по `action`: автоматические
    действия перечислены в `AUTOMATIC_ACTIONS`.
    """
    session.add(StaffActionLog(actor_user_id=None, actor_login=None, **record.model_dump()))


# Действия, которые пишет система, а не сотрудник (spec 0043 §2.3).
AUTOMATIC_ACTIONS: frozenset[StaffAction] = frozenset({StaffAction.STAY_AUTO_CHECKED_OUT})


async def list_staff_actions(
    *,
    day_start: datetime,
    day_end: datetime,
    actor_login: str | None,
    actions: frozenset[StaffAction] | None,
    limit: int,
    offset: int,
) -> list[StaffActionRead]:
    """Строки журнала тенанта в полуинтервале `[day_start, day_end)`, новые сверху.

    Границу суток по поясу отеля считает вызывающая сторона (канон
    `list_requests_closed_since`). `actor_login`/`actions` None — без фильтра.
    Сортировка стабильна (`occurred_at DESC`, tie-break по `id`) — страницы
    `offset` не перекрываются (канон `list_requests`).
    """
    query = select(StaffActionLog).where(
        StaffActionLog.occurred_at >= day_start, StaffActionLog.occurred_at < day_end
    )
    if actor_login is not None:
        query = query.where(StaffActionLog.actor_login == actor_login)
    if actions is not None:
        query = query.where(StaffActionLog.action.in_(actions))
    async with session_scope() as session:
        rows = await session.scalars(
            query.order_by(StaffActionLog.occurred_at.desc(), StaffActionLog.id)
            .limit(limit)
            .offset(offset)
        )
        return [StaffActionRead.model_validate(row) for row in rows]


async def list_staff_action_logins() -> list[str]:
    """Логины, встречающиеся в журнале тенанта, — пункты фильтра «Сотрудник».

    Из журнала, а не из состава команды: отключённый сотрудник остаётся
    в истории, и менеджер должен находить его строки."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(StaffActionLog.actor_login)
            .where(StaffActionLog.actor_login.is_not(None))
            .distinct()
            .order_by(StaffActionLog.actor_login)
        )
        return [login for login in rows if login is not None]


async def delete_staff_actions_before(cutoff: datetime) -> int:
    """Удалить строки журнала тенанта старше `cutoff` (§2.5); вернуть число.

    Идемпотентна (P-8): повторный прогон строк не находит. Зовёт джоба
    ретеншна (`channels/common/retention.py`) внутри `tenant_context`.
    """
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                delete(StaffActionLog).where(StaffActionLog.occurred_at < cutoff)
            ),
        )
    deleted = result.rowcount or 0
    if deleted:
        logger.info("staff_actions_deleted", deleted=deleted)
    return deleted
