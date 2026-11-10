"""Лимит выпуска приглашений по (tenant, менеджер) (spec 0033 §9, #163, канон 0023).

Без него угнанная сессия менеджера штампует одноразовые ссылки без счёта, а каждая
принятая ссылка — новая учётка в отеле. Три двери выпуска — по одному
(`staff_invites.create_invite`), списком (`staff_invite_batch.create_invites`) и
«Новая ссылка» (`staff_invite_reissue.reissue_invite`) — делят один счётчик; вес
события — число выпущенных ссылок.

Бюджет тратит только выпуск: проверка — `peek_rate_limit` до записи, учёт —
`consume_rate_limit` после неё. Иначе список на сотню строк с одной опечаткой
съедал бы сотню за каждую попытку исправить. Проверка и учёт не атомарны —
параллельные запросы проскочат сверх лимита; это тот же осознанный компромисс,
что у входа персонала (#207): потолок ловит скрипт, а не ведёт точный счёт. Лимит
просторный: в день запуска менеджер раздаёт приглашения сотням сотрудников
(spec 0072 §4). Redis недоступен — fail-open, как у всех лимитов (spec 0023 §2).
"""

from __future__ import annotations

import uuid

from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit, peek_rate_limit

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_INVITE_RATE_LIMITED = "ERR-AUTH-019"

_SCOPE = "staff_invite_issue"


def _key(tenant_id: uuid.UUID, actor_user_id: uuid.UUID) -> str:
    # Ключ начинается с tenant_id (P-4: у Redis нет RLS).
    return f"{tenant_id}:{actor_user_id}"


async def ensure_invite_budget(
    tenant_id: uuid.UUID, actor_user_id: uuid.UUID, *, amount: int = 1
) -> None:
    """Отказать ERR-AUTH-019 (429), если `amount` новых ссылок не влезает в окно.

    Список проверяется целиком: не влезает — не выпускается ни одна строка, как
    при плохом логине (spec 0072 §4). Бюджет здесь не тратится — это делает
    `record_invites_issued` после записи."""
    settings = get_settings()
    limit = settings.staff_invite_issue_rate_limit_attempts
    if limit <= 0:
        return
    decision = await peek_rate_limit(
        _SCOPE,
        _key(tenant_id, actor_user_id),
        limit=limit,
        window_seconds=settings.staff_invite_issue_rate_limit_window_seconds,
    )
    if decision.count + amount <= limit:
        return
    logger.warning(
        "staff.invite_rate_limited",
        tenant_id=str(tenant_id),
        actor_user_id=str(actor_user_id),
        requested=amount,
        issued_in_window=decision.count,
        limit=limit,
    )
    raise AppError(
        code=ERR_AUTH_INVITE_RATE_LIMITED,
        message="Too many invites issued by this manager — try again later",
        status_code=429,
    )


async def record_invites_issued(
    tenant_id: uuid.UUID, actor_user_id: uuid.UUID, *, amount: int = 1
) -> None:
    """Учесть выпущенные ссылки — только после того, как они записаны."""
    settings = get_settings()
    limit = settings.staff_invite_issue_rate_limit_attempts
    if limit <= 0:
        return
    await consume_rate_limit(
        _SCOPE,
        _key(tenant_id, actor_user_id),
        limit=limit,
        window_seconds=settings.staff_invite_issue_rate_limit_window_seconds,
        amount=amount,
    )
