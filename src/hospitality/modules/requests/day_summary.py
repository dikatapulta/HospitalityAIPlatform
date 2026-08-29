"""Числа заявок за один день отеля (spec 0035 §6, PR D серии — issue #300).

Отдельный файл, а не ещё сотня строк в `service.py`: тот уже на 484 при
границе R-3 «~400». Вход снаружи — реэкспорт `day_summary` через `api.py`
(контракт 5 import-linter), как у всего остального в модуле.

Правило раздела одно: **каждое число считает владелец своих данных, а сводка
их только складывает** (P-5, §6). Здесь — числа заявок и ничего больше:
эскалации считает `channels/common`, расход на модель — `ai/gateway`, склейка
живёт в кабинете и в утреннем сообщении. Отдельного «модуля отчётов» нет:
витрины и `analytics/` — Phase 4, Phase 1 разрешает простые счётчики и
запрещает аналитику сверх них.

Границы суток — сутки отеля. «Создано» считается сравнением с колонкой
`service_day` (локальная дата отеля, присвоенная вместе с дневным номером
`#N`) — без единого пересчёта поясов; «закрыто» и «взято» — по `closed_at` /
`claimed_at` в границах `[полночь отеля, полночь отеля + 1 день)`, пояс — из
конфига тенанта (§9 FOUNDATION).
"""

from __future__ import annotations

import statistics
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from pydantic import BaseModel
from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.requests.models import (
    RequestCategory,
    RequestStatus,
    ServiceRequest,
    ServiceRequestOrigin,
)
from hospitality.modules.requests.service import _OPEN_STATUSES
from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    TenantConfig,
    load_tenant_config,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Строки выборок этого файла: полей мало и они разные у каждой, поэтому строки,
# а не ORM-объекты (грузить заявку целиком ради пяти колонок незачем).
_CreatedRow = Row[
    tuple[uuid.UUID, ServiceRequestOrigin, datetime, datetime | None, datetime | None]
]
_ClaimedRow = Row[tuple[datetime, datetime | None]]


class DayServiceCounts(BaseModel):
    """Одна строка разреза «по службам»: сколько создано и сколько закрыто.

    Категория — идентификатором, а не названием: имена служб отдаёт
    `list_categories()`, и дублировать их здесь значило бы завести второй
    экземпляр того же факта.
    """

    category_id: uuid.UUID
    created: int
    closed: int


class RequestsDaySummary(BaseModel):
    """Числа заявок за сутки отеля — всё, что модуль знает о своём дне (§6).

    `created_by_origin` содержит ВСЕ значения `ServiceRequestOrigin`, в том
    числе нулевые: у читающего не должно быть повода писать `.get(..., 0)`, а
    сумма значений обязана сходиться с `created_total` — на этом стоит доля
    Exit-критерия Phase 1 (числитель `guest_chat`, знаменатель — «создано»
    целиком, §4). Пока их было два вместо трёх, заявка из публичной двери
    выпадала из обеих частей дроби разом (issue #313).

    `claim_median_seconds` и `overdue` равны `None`, когда числа НЕ СУЩЕСТВУЕТ,
    а не когда оно ноль: некого мерить (в этот день не взяли ни одной заявки)
    и нечем мерить (у отеля выключены напоминания — срока нет). Ноль на их
    месте читался бы как «брали мгновенно» и «сроки соблюдены» — §9 показывает
    оба случая прочерком.

    `by_service` упорядочен по убыванию созданных (дальше — закрытых, дальше —
    id, чтобы порядок был воспроизводим): служба с самой большой нагрузкой
    стоит первой строкой и на странице, и в утреннем сообщении, поэтому
    сортировка одна и живёт здесь, а не в двух показывающих местах. Службы, у
    которых в этот день не было ни создано, ни закрыто, в список не попадают —
    строка «0 → 0» ничего не сообщает.

    `day_start` / `day_end` — окно суток отеля, в котором посчитаны «закрыто» и
    «взято». Они отдаются наружу не для показа, а чтобы остальные владельцы
    чисел сводки (эскалации `channels/common`, расход `ai/gateway`) считали
    СВОИ числа ровно за это же окно, а не за своё, посчитанное из того же
    конфига второй раз: два окна, разъехавшихся на правку часового пояса между
    двумя чтениями, дали бы сводку, которая не сходится сама с собой.
    """

    service_day: date
    day_start: datetime
    day_end: datetime
    created_total: int
    created_by_origin: dict[ServiceRequestOrigin, int]
    closed_total: int
    closed_done: int
    closed_cancelled: int
    by_service: list[DayServiceCounts]
    claim_median_seconds: int | None
    overdue: int | None
    open_now: int


async def day_summary(service_day: date) -> RequestsDaySummary:
    """Собрать числа заявок тенанта за этот день отеля (внутри `tenant_context`).

    Среза (`limit`) здесь нет — в отличие от лент `list_*`, где он страхует от
    неограниченного скана: у счётчика урезанная выборка даёт не «показали не
    всё», а неверное число, и молча. Выборку ограничивают сами сутки одного
    отеля (~50–120 заявок, spec 0033 §1).

    Тенант без конфигурации (онбординг не завершён, служебный smoke-тенант)
    сводку не роняет: пояс деградирует на UTC (канон `_hotel_service_day`), а
    просрочка становится прочерком — сроков у такого отеля нет.
    """
    async with session_scope() as session:
        config = await _tenant_config(session)
        zone: tzinfo = config.tzinfo if config is not None else UTC
        day_start = _local_midnight_utc(service_day, zone)
        day_end = _local_midnight_utc(service_day + timedelta(days=1), zone)

        created_rows: Sequence[_CreatedRow] = (
            await session.execute(
                select(
                    ServiceRequest.category_id,
                    ServiceRequest.origin,
                    ServiceRequest.created_at,
                    ServiceRequest.claimed_at,
                    ServiceRequest.closed_at,
                ).where(ServiceRequest.service_day == service_day)
            )
        ).all()
        closed_rows = (
            await session.execute(
                select(ServiceRequest.category_id, ServiceRequest.status, func.count())
                .where(
                    ServiceRequest.status.not_in(_OPEN_STATUSES),
                    ServiceRequest.closed_at >= day_start,
                    ServiceRequest.closed_at < day_end,
                )
                .group_by(ServiceRequest.category_id, ServiceRequest.status)
            )
        ).all()
        claimed_rows: Sequence[_ClaimedRow] = (
            await session.execute(
                select(ServiceRequest.created_at, ServiceRequest.claimed_at).where(
                    ServiceRequest.claimed_at >= day_start,
                    ServiceRequest.claimed_at < day_end,
                )
            )
        ).all()
        open_now = await session.scalar(
            select(func.count())
            .select_from(ServiceRequest)
            .where(ServiceRequest.status.in_(_OPEN_STATUSES))
        )
        category_keys = await _category_keys(session)

    origin_counts: Counter[ServiceRequestOrigin] = Counter(row.origin for row in created_rows)
    created_by_category: Counter[uuid.UUID] = Counter(row.category_id for row in created_rows)
    closed_by_category: Counter[uuid.UUID] = Counter()
    closed_by_status: Counter[RequestStatus] = Counter()
    for category_id, status, count in closed_rows:
        closed_by_category[category_id] += count
        closed_by_status[status] += count

    return RequestsDaySummary(
        service_day=service_day,
        day_start=day_start,
        day_end=day_end,
        created_total=len(created_rows),
        created_by_origin={origin: origin_counts[origin] for origin in ServiceRequestOrigin},
        closed_total=sum(closed_by_status.values()),
        closed_done=closed_by_status[RequestStatus.DONE],
        closed_cancelled=closed_by_status[RequestStatus.CANCELLED],
        by_service=_by_service(created_by_category, closed_by_category),
        claim_median_seconds=_claim_median_seconds(
            # `claimed_at is not None` гарантирует WHERE выборки; условие стоит
            # здесь ради типов — колонка NULLABLE, и без него читающий не увидит,
            # почему вычитание безопасно.
            [
                claimed_at - created_at
                for created_at, claimed_at in claimed_rows
                if claimed_at is not None
            ]
        ),
        overdue=_overdue_count(created_rows, category_keys, config, day_end=day_end),
        open_now=open_now or 0,
    )


def _by_service(
    created_by_category: Counter[uuid.UUID], closed_by_category: Counter[uuid.UUID]
) -> list[DayServiceCounts]:
    """Разрез «по службам»: объединение служб, у которых в этот день было хоть
    что-то, по убыванию созданных (порядок — докстринг `RequestsDaySummary`)."""
    rows = [
        DayServiceCounts(
            category_id=category_id,
            created=created_by_category[category_id],
            closed=closed_by_category[category_id],
        )
        for category_id in created_by_category.keys() | closed_by_category.keys()
    ]
    rows.sort(key=lambda row: (-row.created, -row.closed, str(row.category_id)))
    return rows


def _local_midnight_utc(day: date, zone: tzinfo) -> datetime:
    """Полночь этой локальной даты отеля в UTC — граница окна суток.

    Обе границы считаются так, а не как «начало + 24 часа»: в поясе с переводом
    стрелок сутки бывают 23- и 25-часовыми, и арифметика в UTC потеряла бы или
    посчитала дважды час заявок. У пилота (Алматы, UTC+5) перевода нет, но
    правило пишется один раз и на все отели.
    """
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def _claim_median_seconds(claim_waits: Sequence[timedelta]) -> int | None:
    """Медиана времени взятия по заявкам, ВЗЯТЫМ в этот день; `None` — не брали.

    Медиана, а не среднее (§6): одна заявка, взятая через сутки, сдвигает
    среднее так, что число перестаёт что-либо значить. Считается в Python, а не
    `percentile_cont` в SQL: строк — дневная выборка одного отеля, а читать это
    место будет сессия, которой понятность дороже экзотики (P-1).
    """
    if not claim_waits:
        return None
    return int(statistics.median(wait.total_seconds() for wait in claim_waits))


def _overdue_count(
    created_rows: Sequence[_CreatedRow],
    category_keys: dict[uuid.UUID, str],
    config: TenantConfig | None,
    *,
    day_end: datetime,
) -> int | None:
    """Сколько заявок этого дня просрочку УСПЕЛИ получить; `None` — сроков нет.

    Предикат целиком — §6: срок `reminder_delay_for(категория)` задан, и либо
    заявку взяли позже срока (`claimed_at - created_at >= delay`), либо не взяли
    вовсе и до отсечки прошло не меньше срока. Отсечка — `min(конец суток отеля,
    сейчас, closed_at)`: у прошедшего дня число уже не меняется, у сегодняшнего
    растёт вместе с днём, у закрытой невзятой останавливается в момент закрытия.

    Отдельного исключения по отмене здесь нет и не нужно: закрытие невзятой
    заявки всегда означает отмену (`done` достижим только из `in_progress`),
    отменённая в первые секунды до срока не доживает, а отменённая через час
    после срока просрочена и считается — это ровно тот случай, ради которого
    число считают (гость не дождался и отменил сам).

    Со сроком бейджа в очереди (`staff_portal/queue.py::_is_overdue`) совпадает
    срок, но не предикат: бейдж — про «сейчас» и гаснет, когда заявку взяли;
    здесь — про «случилось», и просрочка вчерашнего дня остаётся в сводке того
    дня навсегда. Числа поэтому законно расходятся, и плитка называется
    «Просрочено за день», а не «Просрочено».

    Отель с выключенными напоминаниями (и тенант без конфига) просрочек не
    имеет как явления — прочерк, а не ноль.
    """
    if config is None or config.min_reminder_delay() is None:
        return None
    now = utc_now()
    overdue = 0
    for row in created_rows:
        delay = config.reminder_delay_for(category_keys.get(row.category_id))
        if delay is None:
            continue
        if row.claimed_at is not None:
            waited = row.claimed_at - row.created_at
        else:
            cutoff = min(day_end, now)
            if row.closed_at is not None:
                cutoff = min(cutoff, row.closed_at)
            waited = cutoff - row.created_at
        if waited >= delay:
            overdue += 1
    return overdue


async def _category_keys(session: AsyncSession, /) -> dict[uuid.UUID, str]:
    """`category_id` → `key` категории: срок напоминания задан по ключу, а у
    заявки в руках только id (RLS ограничивает выборку текущим тенантом)."""
    rows = (await session.execute(select(RequestCategory.id, RequestCategory.key))).all()
    return {category_id: key for category_id, key in rows}


async def _tenant_config(session: AsyncSession, /) -> TenantConfig | None:
    """Конфиг отеля: пояс суток и сроки напоминаний — одним чтением.

    Незавершённый онбординг — `None`, а не отказ (канон `_hotel_service_day`).
    Остальные ошибки чтения пробрасываются намеренно: «конфиг в БД не проходит
    схему» — дрейф данных, и он обязан быть громким (канон очереди кабинета).
    """
    try:
        return await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        logger.warning("day_summary_tenant_config_missing", error_code=error.code)
        return None
