"""Просроченные проживания: автовыезд по сроку и выборка для кабинета (spec 0043, #225).

Выезд оформляет человек; забытый выезд держал бы Stay в `checked_in` вечно, и
partial unique «один активный Stay на комнату» отказывал бы следующему
заселению (ERR-GUESTS-002). Поэтому воркер раз в
`worker_stay_auto_checkout_interval_seconds` зовёт `close_overdue_stays`: Stay,
у которого с `check_out_at` прошёл запас тенанта
(`TenantConfig.stay_auto_checkout_grace_hours`), закрывается тем же путём, что
ручной выезд (`service.close_stay`, P-12). Гостю это ничего не меняет: доступ
погас ещё в `check_out_at` (ADR-008 Q8).

Механика — канон периодической задачи `remind_unclaimed_requests` (NG-8):
список тенантов — платформенной сессией, работа со Stay — внутри
`tenant_context` каждого (P-4); сбой одного отеля не останавливает остальных.
Каждый Stay — своя транзакция: строка берётся `FOR UPDATE SKIP LOCKED` с
перепроверкой статуса и срока, так что сотрудник, продлевающий Stay в ту же
секунду, либо успевает первым, либо получает честный ERR-GUESTS-001
(`service._get_active_stay_or_raise(..., for_update=True)`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final

import structlog
from sqlalchemy import select

from hospitality.modules.guests.models import Stay, StayStatus
from hospitality.modules.guests.schemas import StayRead
from hospitality.modules.guests.service import close_stay
from hospitality.platform.config import list_configured_tenant_ids, load_tenant_config
from hospitality.platform.staff_actions import (
    StaffAction,
    StaffActionRecord,
    record_automatic_action,
)
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_stays_auto_checked_out
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): прогон автовыезда упал.
ERR_GUESTS_AUTO_CHECKOUT_FAILED = "ERR-GUESTS-007"

# Потолок Stay на отель за один прогон: 350 номеров отеля дают запас. Остаток
# не теряется — его подберёт следующий прогон через интервал воркера.
_SCAN_LIMIT: Final = 500

# Потолок списка «Не выехали в срок» в кабинете: больше страница не покажет
# осмысленно, а ресепшен разбирает его сверху — от самых давних.
_OVERDUE_LIST_LIMIT: Final = 100


async def close_overdue_stays() -> int:
    """Один прогон: закрыть Stay всех тенантов, у которых вышел запас. Вернуть число.

    Зовётся из цикла воркера (`hospitality/worker.py`) и тестами напрямую.
    Свой `correlation_id` (§10.2): входящего запроса у фоновой задачи нет.
    """
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        async with platform_session_scope() as session:
            tenant_ids = await list_configured_tenant_ids(session)
        closed = 0
        for tenant_id in tenant_ids:
            try:
                closed += await _close_for_tenant(tenant_id)
            except Exception:
                # Сбой у одного отеля (битые данные, БД мигнула) не отменяет
                # автовыезд у остальных: скан обязан обойти всех.
                logger.error(
                    "stay_auto_checkout_failed",
                    error_code=ERR_GUESTS_AUTO_CHECKOUT_FAILED,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
        logger.info("stay_auto_checkout_scanned", tenants=len(tenant_ids), closed=closed)
        return closed


async def list_overdue_stays(*, now: datetime | None = None) -> list[StayRead]:
    """Stay в `checked_in` со сроком выезда в прошлом — для кабинета, давние сверху.

    Взгляд ПЕРСОНАЛА в контексте тенанта запроса (P-4): запас автовыезда здесь
    не учитывается — Stay просрочен с `check_out_at`, а не с момента автовыезда.
    """
    async with session_scope() as session:
        stays = await session.scalars(
            select(Stay)
            .where(Stay.status == StayStatus.CHECKED_IN, Stay.check_out_at <= (now or utc_now()))
            .order_by(Stay.check_out_at, Stay.room_number)
            .limit(_OVERDUE_LIST_LIMIT)
        )
        return [StayRead.model_validate(stay) for stay in stays]


async def _close_for_tenant(tenant_id: uuid.UUID) -> int:
    """Автовыезд одного тенанта; вернуть число закрытых Stay."""
    # tenant_id — явно: до входа в tenant_context его в лог-контексте нет (§10.1).
    with structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)):
        try:
            async with platform_session_scope() as session:
                config = await load_tenant_config(session, tenant_id)
        except AppError as error:
            # Дрейф схемы конфига или гонка с удалением тенанта: этот отель
            # пропускаем, остальных скан обязан обойти.
            logger.warning("stay_auto_checkout_config_unavailable", error_code=error.code)
            return 0
        grace = config.stay_auto_checkout_grace()
        if grace is None:
            return 0  # автовыезд у этого отеля выключен (spec 0043 §2.1)

        with tenant_context(tenant_id):
            cutoff = utc_now() - grace
            async with session_scope() as session:
                stay_ids = list(
                    await session.scalars(
                        select(Stay.id)
                        .where(Stay.status == StayStatus.CHECKED_IN, Stay.check_out_at <= cutoff)
                        .order_by(Stay.check_out_at)
                        .limit(_SCAN_LIMIT)
                    )
                )
            if len(stay_ids) == _SCAN_LIMIT:
                # Усечённая выборка выглядела бы как «всё закрыто».
                logger.warning("stay_auto_checkout_truncated", limit=_SCAN_LIMIT)
            closed = 0
            for stay_id in stay_ids:
                if await _close_one(stay_id, cutoff=cutoff, grace=grace):
                    closed += 1
            if closed:
                record_stays_auto_checked_out(closed)
                logger.info("stays_auto_checked_out", closed=closed)
        return closed


async def _close_one(stay_id: uuid.UUID, *, cutoff: datetime, grace: timedelta) -> bool:
    """Закрыть один Stay своей транзакцией; False — его уже взял или сдвинул сотрудник.

    `SKIP LOCKED` + перепроверка условия в той же выборке: строку, которую
    прямо сейчас продлевают или выписывают, джоба не ждёт и не трогает —
    следующий прогон увидит её уже в новом состоянии.
    """
    async with session_scope() as session:
        stay = await session.scalar(
            select(Stay)
            .where(
                Stay.id == stay_id,
                Stay.status == StayStatus.CHECKED_IN,
                Stay.check_out_at <= cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        if stay is None:
            return False
        await close_stay(session, stay, utc_now())
        record_automatic_action(
            session,
            StaffActionRecord(
                action=StaffAction.STAY_AUTO_CHECKED_OUT,
                stay_id=stay.id,
                room_number=stay.room_number,
            ),
        )
    logger.info(
        "stay_auto_checked_out",
        stay_id=str(stay.id),
        room_number=stay.room_number,
        grace_hours=int(grace.total_seconds() // 3600),
    )
    return True
