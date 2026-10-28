"""Отказы модели по тенантам — снимок для алертера (issue #374).

Отдельный файл по той же причине, что `spend.py`: `service.py` уже за границей
R-3 «~400 строк». Вход снаружи — реэкспорт через `api.py` (R-5, §7.2).

Два вопроса, на которые отвечает снимок, и оба — по журналу `llm_call_log`, а
не по счётчику `llm_calls_total`: счётчик живёт в памяти процесса, и вызовы
модели из воркера (перевод сути заявки) в `/metrics` приложения не попадают,
а рестарт обнуляет его вместе с базовой линией. Журнал общий у всех процессов.

1. **Провайдер отказывает** (ERR-AI-001/003 → алерт ERR-OPS-009): сколько
   вызовов провайдера неуспешны ПОСЛЕ последнего успешного. Не «за окно
   времени»: на пилоте ночью сообщение в час, днём — в минуту, и окно, годное
   для одного, глухо или шумно для другого. Серия же не зависит от трафика:
   три гостя подряд без ответа — это три, когда бы они ни написали; одиночный
   сбой среди успехов серию не набирает. Отказы по бюджету в серию не входят —
   до провайдера они не доходили и о нём ничего не говорят.
2. **Бюджет исчерпан** (ERR-AI-002 → алерт ERR-OPS-010): сколько вызовов
   отвергнуто бюджетом подряд — после последнего прошедшего бюджет — в текущих
   UTC-сутках. Больше нуля — бот сейчас молчит. Окно — сутки бюджета
   (`_utc_day_start`): с полуночи UTC бюджет обнулился, и вчерашние отказы
   о сегодняшнем дне не говорят.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.ai.gateway.models import LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.service import _utc_day_start
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import LlmCallOutcomes, set_llm_call_outcomes
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)


async def refresh_call_outcome_metrics() -> None:
    """Опубликовать в ``/metrics`` отказы модели каждого тенанта (issue #374).

    Устроено как ``refresh_budget_metrics`` (#103) и по тем же причинам: зовётся
    на каждый scrape (подключение — ``app.py``), запрос — на тенанта, потому что
    журнал под RLS и платформенная сессия его не видит (ADR-003). Недоступная БД
    стирает снимок — для алертера это «не знаю», и он молчит.
    """
    try:
        async with platform_session_scope() as session:
            tenant_ids = list(await session.scalars(select(Tenant.id)))
        snapshot = {}
        for tenant_id in tenant_ids:
            with tenant_context(tenant_id):
                async with session_scope() as session:
                    snapshot[str(tenant_id)] = await _call_outcomes(session)
    except Exception:  # диагностический путь: /metrics обязан отдаваться и без БД
        logger.warning("llm_call_outcomes_unavailable", exc_info=True)
        set_llm_call_outcomes({})
        return
    set_llm_call_outcomes(snapshot)


async def _call_outcomes(session: AsyncSession) -> LlmCallOutcomes:
    """Обе серии ТЕКУЩЕГО тенанта (тенантная сессия, RLS — P-4)."""
    last_ok_at = (
        select(func.max(LlmCallLog.created_at))
        .where(LlmCallLog.status == LlmCallStatus.OK)
        .scalar_subquery()
    )
    failures = dict(
        (
            await session.execute(
                select(LlmCallLog.status, func.count())
                .where(
                    LlmCallLog.status.in_([LlmCallStatus.ERROR, LlmCallStatus.TIMEOUT]),
                    # Успешного вызова не было никогда — в серии весь журнал.
                    or_(last_ok_at.is_(None), LlmCallLog.created_at > last_ok_at),
                )
                .group_by(LlmCallLog.status)
            )
        )
        .tuples()
        .all()
    )

    day_start = _utc_day_start()
    last_passed_at = (
        select(func.max(LlmCallLog.created_at))
        .where(
            LlmCallLog.status != LlmCallStatus.BUDGET_EXCEEDED,
            LlmCallLog.created_at >= day_start,
        )
        .scalar_subquery()
    )
    budget_rejections = await session.scalar(
        select(func.count()).where(
            LlmCallLog.status == LlmCallStatus.BUDGET_EXCEEDED,
            LlmCallLog.created_at >= day_start,
            or_(last_passed_at.is_(None), LlmCallLog.created_at > last_passed_at),
        )
    )
    return LlmCallOutcomes(
        provider_errors=failures.get(LlmCallStatus.ERROR, 0),
        provider_timeouts=failures.get(LlmCallStatus.TIMEOUT, 0),
        budget_rejections=budget_rejections or 0,
    )
