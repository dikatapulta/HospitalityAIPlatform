"""Расход на модель за произвольное окно (spec 0035 §6, issue #301).

Отдельный файл, а не ещё двадцать строк в `service.py`: тот и без них 540 строк
при границе R-3 «~400» (канон — `modules/requests/day_summary.py`, заведённый
по той же причине). Вход снаружи — реэкспорт через `api.py` (R-5, §7.2), как у
всего остального в пакете.

Правило то же, что у остальных чисел сводки дня: **число считает владелец своих
данных** (P-5, §6). Журнал вызовов `llm_call_log` живёт здесь, значит и сумма по
нему — здесь; сводка её только складывает с числами других владельцев.

Не путать с дневным бюджетом (`service.py::_daily_spent_usd`, ADR-017): у того
окно всегда UTC-сутки и своё назначение — отсечка лимита. Здесь окно приходит
от вызывающего, и вызывающий — сводка, то есть сутки ОТЕЛЯ. У пилота в Алматы
(UTC+5) окна разъезжаются на пять часов; расхождение названо в §6 спеки и
является свойством, а не дефектом: одно число отвечает «во что обошёлся день
отеля», другое — «сколько осталось до отсечки».
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from hospitality.ai.gateway.models import LlmCallLog
from hospitality.shared.db import session_scope


async def spend_usd_between(*, created_after: datetime, created_before: datetime) -> Decimal:
    """Во сколько обошлись вызовы LLM текущего тенанта в границах окна.

    Строка «ИИ за сутки отеля: $1.84» в копии сводки основателю (spec 0035 §8).
    Границы окна задаёт вызывающая сторона — gateway про часовые пояса показа не
    знает (канон `count_escalations` в `channels/common`, `list_requests_closed_since`
    в `modules/requests`). Окно полуоткрытое `[created_after, created_before)` —
    иначе вызов ровно в полночь попал бы сразу в два дня.

    Считаются ВСЕ строки журнала, а не только успешные: у исчерпанных таймаутов
    и ошибок провайдера `cost_usd` равен нулю (`_log_call`), поэтому фильтр по
    статусу ничего не изменил бы, а появись когда-нибудь платный неуспех — он
    обязан попасть в счёт, а не потеряться.

    Зовётся внутри `tenant_context`; чужой расход отсекает RLS (P-4). Сумма
    пустого окна — `Decimal(0)`, а не `None`: «вызовов не было» — это ноль
    долларов, и вызывающему не с чем разбираться.
    """
    async with session_scope() as session:
        spent = await session.scalar(
            select(func.coalesce(func.sum(LlmCallLog.cost_usd), 0)).where(
                LlmCallLog.created_at >= created_after,
                LlmCallLog.created_at < created_before,
            )
        )
    return Decimal(spent if spent is not None else 0)
