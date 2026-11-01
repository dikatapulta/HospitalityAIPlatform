"""Сверка проживаний со снимком PMS — шаг воркера (spec 0058 §2.2–§2.6, ADR-004).

Раз в `worker_pms_sync_interval_seconds` воркер зовёт `sync_stays_from_pms`.
Механика — канон `close_overdue_stays` (spec 0043): тенанты — платформенной
сессией, работа — в `tenant_context` каждого (P-4), сбой одного отеля не
останавливает остальных (ERR-GUESTS-008). Сверка идёт только у тенанта с
`TenantConfig.pms_source: opera_report`.

Прогон тенанта: снимок из порта → свежесть (ERR-GUESTS-010) → тот же
`source_id`, что применён, — ноль действий (п. 6) → план (`pms_plan.py`) →
шаги по одному Stay в транзакции (`pms_apply.py`) → одной транзакцией в конце
счётчики пропусков, применённый `source_id`, придержанные выезды и расхождения.
Оборванный посреди прогон не записал конец — следующий повторит снимок целиком,
а уже сделанные шаги план просто не найдёт: пропуск не считается дважды.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.guests.models import PmsSyncState, Stay, StaySource, StayStatus
from hospitality.modules.guests.pms_apply import (
    apply_adopt,
    apply_check_in,
    apply_check_out,
    apply_extend,
    apply_move,
)
from hospitality.modules.guests.pms_plan import HouseStay, SyncPlan, plan_sync
from hospitality.modules.guests.pms_status import pms_stale_after
from hospitality.platform.config import (
    TenantConfig,
    list_configured_tenant_ids,
    load_tenant_config,
)
from hospitality.shared.alerting import NO_CORRELATION_ID, AlertSender, format_alert
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_pms_sync_action, set_pms_snapshot_ages
from hospitality.shared.ports.pms import PmsSnapshot, get_pms
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_GUESTS_PMS_SYNC_FAILED = "ERR-GUESTS-008"  # прогон сверки упал
ERR_GUESTS_PMS_CLOSURES_HELD = "ERR-GUESTS-009"  # выезды придержаны защитой, алерт
ERR_GUESTS_PMS_SNAPSHOT_STALE = "ERR-GUESTS-010"  # снимок устарел, алерт


async def sync_stays_from_pms(*, alert_sender: AlertSender | None = None) -> int:
    """Один прогон сверки по всем тенантам с Opera; вернуть число сделанных шагов.

    `alert_sender` — тракт алертов команды (канон `remind_unclaimed_requests`):
    кладёт композиционный корень, None — алерты не настроены, только логи.
    """
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        async with platform_session_scope() as session:
            tenant_ids = await list_configured_tenant_ids(session)
        applied = 0
        for tenant_id in tenant_ids:
            try:
                applied += await _sync_tenant(tenant_id, alert_sender)
            except Exception:
                # Битые данные или мигнувшая БД одного отеля не отменяют сверку у остальных.
                logger.error(
                    "pms_sync_failed",
                    error_code=ERR_GUESTS_PMS_SYNC_FAILED,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
        return applied


async def refresh_pms_snapshot_age_metrics() -> None:
    """Возраст последнего снимка каждого тенанта с Opera — в `/metrics` (§2.6).

    Обновлятор scrape: подключает `app.py` (`register_scrape_refresher`), снимок
    читает тот же порт, что сверка.
    """
    async with platform_session_scope() as session:
        tenant_ids = await list_configured_tenant_ids(session)
    now = utc_now()
    ages: dict[str, float] = {}
    for tenant_id in tenant_ids:
        if await _opera_config(tenant_id) is None:
            continue
        with tenant_context(tenant_id):
            snapshot = await get_pms().fetch_snapshot(tenant_id)
        if snapshot is not None:
            ages[str(tenant_id)] = (now - snapshot.taken_at).total_seconds()
    set_pms_snapshot_ages(ages)


async def _sync_tenant(tenant_id: uuid.UUID, alert_sender: AlertSender | None) -> int:
    """Сверка одного тенанта; вернуть число сделанных шагов."""
    # tenant_id — явно: до входа в tenant_context его в лог-контексте нет (§10.1).
    with structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)):
        config = await _opera_config(tenant_id)
        if config is None:
            return 0
        with tenant_context(tenant_id):
            snapshot = await get_pms().fetch_snapshot(tenant_id)
            if snapshot is None:
                return 0  # снимков ещё не было: кабинет говорит «ещё не приходили» (§2.5)
            await _watch_freshness(tenant_id, snapshot, config, alert_sender)
            async with session_scope() as session:
                state = await session.get(PmsSyncState, tenant_id)
                if state is not None and state.last_applied_source_id == snapshot.source_id:
                    return 0  # тот же снимок — ноль действий (ADR-004 п. 6)
                was_held = state is not None and state.held_closures > 0
                house = await _load_house(session)
                closed_refs = await _closed_booking_refs(session, snapshot)
            plan = plan_sync(
                snapshot.rows,
                house,
                closed_refs,
                zone=config.tzinfo,
                checkout_hour=config.stay_checkout_hour,
                now=utc_now(),
            )
            applied = await _execute(plan)
            await _finish(tenant_id, snapshot, plan)
            if plan.held_closures:
                pms_stays = sum(1 for stay in house if stay.source is StaySource.PMS)
                await _report_held(tenant_id, plan, pms_stays, alert_sender, alert=not was_held)
            logger.info(
                "pms_snapshot_applied",
                rows=len(snapshot.rows),
                applied=applied,
                discrepancies=len(plan.discrepancies),
                held_closures=plan.held_closures,
            )
            return applied


async def _opera_config(tenant_id: uuid.UUID) -> TenantConfig | None:
    """Конфиг тенанта со сверкой; None — у отеля `manual` или конфиг не читается."""
    try:
        async with platform_session_scope() as session:
            config = await load_tenant_config(session, tenant_id)
    except AppError as error:
        # Дрейф схемы конфига или гонка с удалением тенанта: отель пропускаем.
        logger.warning(
            "pms_sync_config_unavailable", error_code=error.code, tenant_id=str(tenant_id)
        )
        return None
    return config if config.pms_source == "opera_report" else None


async def _load_house(session: AsyncSession) -> list[HouseStay]:
    stays = await session.scalars(select(Stay).where(Stay.status == StayStatus.CHECKED_IN))
    return [
        HouseStay(
            id=stay.id,
            room_number=stay.room_number,
            source=stay.source,
            booking_ref=stay.pms_booking_ref,
            check_in_at=stay.check_in_at,
            check_out_at=stay.check_out_at,
            missed_snapshots=stay.pms_missed_snapshots,
        )
        for stay in stays
    ]


async def _closed_booking_refs(session: AsyncSession, snapshot: PmsSnapshot) -> frozenset[str]:
    """Брони снимка, чей Stay уже закрыт: заново не заводятся (п. 1)."""
    refs = [row.booking_ref for row in snapshot.rows]
    if not refs:
        return frozenset()
    closed = await session.scalars(
        select(Stay.pms_booking_ref)
        .where(Stay.pms_booking_ref.in_(refs), Stay.status != StayStatus.CHECKED_IN)
        .distinct()
    )
    return frozenset(ref for ref in closed if ref is not None)


async def _execute(plan: SyncPlan) -> int:
    """Шаги плана по порядку §2.2: выезды, переселения, сроки, заезды."""
    done = {
        "check_out": sum([await apply_check_out(step) for step in plan.check_outs]),
        "move": sum([await apply_move(step) for step in plan.moves]),
        "extend": sum([await apply_extend(step) for step in plan.extends]),
        "check_in": sum([await apply_check_in(step) for step in plan.check_ins]),
        "link": sum([await apply_adopt(step) for step in plan.adoptions]),
    }
    for action, count in done.items():
        if count:
            record_pms_sync_action(action, count)
    return sum(done.values())


async def _finish(tenant_id: uuid.UUID, snapshot: PmsSnapshot, plan: SyncPlan) -> None:
    """Конец прогона одной транзакцией: счётчики пропусков и состояние сверки (§2.2)."""
    async with session_scope() as session:
        if plan.add_missed:
            await session.execute(
                update(Stay)
                .where(
                    Stay.id.in_(plan.add_missed),
                    Stay.status == StayStatus.CHECKED_IN,
                    Stay.source == StaySource.PMS,
                )
                .values(pms_missed_snapshots=Stay.pms_missed_snapshots + 1)
            )
        if plan.reset_missed:
            await session.execute(
                update(Stay).where(Stay.id.in_(plan.reset_missed)).values(pms_missed_snapshots=0)
            )
        state = await _state(session, tenant_id)
        state.last_applied_source_id = snapshot.source_id
        state.last_applied_at = utc_now()
        state.held_closures = plan.held_closures
        state.discrepancies = [item.model_dump(mode="json") for item in plan.discrepancies]


async def _watch_freshness(
    tenant_id: uuid.UUID,
    snapshot: PmsSnapshot,
    config: TenantConfig,
    alert_sender: AlertSender | None,
) -> None:
    """Эпизод «снимок устарел» (§2.6): алерт на входе, «снова приходит» — на выходе."""
    interval = config.pms_snapshot_interval_minutes
    age = utc_now() - snapshot.taken_at
    stale = age > pms_stale_after(interval)
    async with session_scope() as session:
        state = await _state(session, tenant_id)
        if stale == (state.stale_alerted_at is not None):
            return
        state.stale_alerted_at = utc_now() if stale else None
    minutes = int(age.total_seconds() // 60)
    if stale:
        logger.warning(
            "pms_snapshot_stale", error_code=ERR_GUESTS_PMS_SNAPSHOT_STALE, age_minutes=minutes
        )
        title = "данные из Opera не обновляются"
        detail = (
            f"последний снимок — {minutes} мин назад, отель шлёт раз в {interval} мин; "
            "новые заселения в кабинет сами не попадут"
        )
    else:
        logger.info("pms_snapshot_fresh_again", age_minutes=minutes)
        title, detail = "данные из Opera снова приходят", f"свежий снимок — {minutes} мин назад"
    await _alert(
        alert_sender,
        tenant_id,
        error_code=ERR_GUESTS_PMS_SNAPSHOT_STALE,
        title=title,
        detail=detail,
        emoji="🔴" if stale else "✅",
    )


async def _report_held(
    tenant_id: uuid.UUID,
    plan: SyncPlan,
    pms_stays: int,
    alert_sender: AlertSender | None,
    *,
    alert: bool,
) -> None:
    """Выезды придержаны (§2.3): WARNING на каждый такой снимок, алерт — один на эпизод."""
    logger.warning(
        "pms_closures_held",
        error_code=ERR_GUESTS_PMS_CLOSURES_HELD,
        held=plan.held_closures,
        pms_stays=pms_stays,
    )
    if alert:
        await _alert(
            alert_sender,
            tenant_id,
            error_code=ERR_GUESTS_PMS_CLOSURES_HELD,
            title="выезды из Opera придержаны",
            detail=(
                f"в выгрузке нет сразу {plan.held_closures} из {pms_stays} проживаний из "
                "Opera — похоже, файл неполный; выезды по ней не оформлены"
            ),
        )


async def _alert(
    alert_sender: AlertSender | None,
    tenant_id: uuid.UUID,
    *,
    error_code: str,
    title: str,
    detail: str,
    emoji: str = "🔴",
) -> None:
    """Сообщение в канал команды; не ушло — ERROR в лог (Sentry), сверка идёт дальше."""
    if alert_sender is None:
        return
    settings = get_settings()
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    text = format_alert(
        error_code=error_code,
        title=title,
        detail=detail,
        environment=settings.sentry_environment,
        runbook_url=settings.alert_runbook_url,
        emoji=emoji,
        tenant=str(tenant_id),
        correlation_id=str(correlation_id or NO_CORRELATION_ID),
    )
    try:
        await alert_sender(text)
    except Exception:
        logger.error("pms_alert_failed", error_code=error_code, exc_info=True)


async def _state(session: AsyncSession, tenant_id: uuid.UUID) -> PmsSyncState:
    """Строка состояния тенанта; первой сверкой — заводится."""
    state = await session.get(PmsSyncState, tenant_id)
    if state is None:
        state = PmsSyncState(tenant_id=tenant_id, held_closures=0, discrepancies=[])
        session.add(state)
    return state
