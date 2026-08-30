"""Числа сводки дня (spec 0035 §6, issue #300) — блок «Сводка (числа)» §13.

Все сценарии строятся сдвигом меток времени и `service_day` прямо в БД
(`shift_request`, канон `test_retention`): ждать реальные сутки нельзя, а
подменять «сейчас» значило бы проверять не тот код, который поедет.

Часовой пояс отеля везде Asia/Almaty (UTC+5) — тот же, что у пилота. Метки
времени по умолчанию ставятся в полдень отеля: он далеко от обеих границ, и
сценарий не зависит от того, в котором часу идёт прогон.

Подмени пояс отеля на UTC — по всему репозиторию краснеют ровно два теста
отсюда: `test_night_hours_belong_to_the_hotel_day_not_to_the_utc_one` (переезжает
день «закрыто» и медианы) и `test_overdue_of_a_past_day_stops_at_its_midnight`
(сдвигается конец суток, на котором стоит вся отсечка просрочки). Остальные при
такой подмене остаются зелёными, и это нормально: полдень отеля лежит и внутри
суток UTC, поэтому их предмета подмена не касается.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from hospitality.modules.requests.api import (
    RequestStatus,
    ServiceRequestCreate,
    ServiceRequestOrigin,
    change_request_status,
    create_request,
    day_summary,
)
from hospitality.modules.requests.tests.conftest import (
    make_category,
    shift_request,
    store_hotel_config,
)
from hospitality.shared.db import utc_now
from hospitality.shared.tenancy import tenant_context

HOTEL_ZONE = ZoneInfo("Asia/Almaty")


def hotel_today() -> date:
    """Сегодняшняя локальная дата отеля — та же, что присваивает `create_request`."""
    return utc_now().astimezone(HOTEL_ZONE).date()


def hotel_moment(day: date, hour: int, minute: int = 0) -> datetime:
    """Момент суток отеля — в UTC, как в БД. Сценарии границ формулируются в
    часах отеля («час ночи 17-го»), а пересчёт в UTC («20:00 16-го») делает
    пояс, а не автор теста руками."""
    return datetime.combine(day, time(hour, minute), tzinfo=HOTEL_ZONE)


def hotel_noon(day: date) -> datetime:
    """Полдень этого дня отеля в UTC: заведомо внутри суток и далеко от обеих
    границ — сдвиг меток не должен зависеть от того, в котором часу идёт тест."""
    return hotel_moment(day, 12)


async def make_request(
    tenant_id: uuid.UUID,
    category_id: uuid.UUID,
    *,
    summary: str,
    origin: ServiceRequestOrigin = ServiceRequestOrigin.GUEST_CHAT,
) -> uuid.UUID:
    with tenant_context(tenant_id):
        created = await create_request(
            ServiceRequestCreate(category_id=category_id, origin=origin, summary=summary)
        )
    return created.id


async def test_created_yesterday_closed_today_lands_in_different_days(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §6: сводка описывает день, а не жизнь заявки. Заявка, созданная
    вчера и закрытая сегодня, стоит в «создано» вчера и в «закрыто» сегодня —
    иначе вчерашний день переписывался бы задним числом каждым закрытием."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a)
    category = await make_category(tenant_a)
    today = hotel_today()
    yesterday = today - timedelta(days=1)

    request_id = await make_request(tenant_a, category.id, summary="вчерашняя")
    with tenant_context(tenant_a):
        await shift_request(request_id, service_day=yesterday, created_at=hotel_noon(yesterday))
        await change_request_status(request_id, RequestStatus.IN_PROGRESS)
        await change_request_status(request_id, RequestStatus.DONE)

        yesterday_summary = await day_summary(yesterday)
        today_summary = await day_summary(today)

    assert (yesterday_summary.created_total, yesterday_summary.closed_total) == (1, 0)
    assert (today_summary.created_total, today_summary.closed_total) == (0, 1)
    assert today_summary.closed_done == 1
    assert today_summary.closed_cancelled == 0


async def test_night_hours_belong_to_the_hotel_day_not_to_the_utc_one(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §6: границы окна — полночь ОТЕЛЯ, а не полночь UTC. Заявку
    взяли и закрыли в час ночи по Алматы — по UTC это ещё предыдущие сутки, и
    оба числа обязаны встать в день отеля, а не в день UTC.

    День берётся фиксированной датой прошлого, а не «сегодня»: окна двух поясов
    должны расходиться в любой час прогона. Подмени пояс отеля на UTC — «закрыто»
    и медиана переедут на день назад, и оба утверждения ниже станут ложными.
    """
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a)
    category = await make_category(tenant_a)
    hotel_day = date(2026, 5, 17)
    utc_day = hotel_day - timedelta(days=1)
    closed_at = hotel_moment(hotel_day, 1)
    # Предпосылка сценария: час ночи 17-го по отелю — это 20:00 16-го по UTC.
    assert closed_at.astimezone(UTC).date() == utc_day

    request_id = await make_request(tenant_a, category.id, summary="ночная")
    with tenant_context(tenant_a):
        await change_request_status(request_id, RequestStatus.IN_PROGRESS)
        await change_request_status(request_id, RequestStatus.DONE)
        await shift_request(
            request_id,
            service_day=hotel_day,
            created_at=hotel_moment(hotel_day, 0, 20),
            claimed_at=hotel_moment(hotel_day, 0, 30),
            closed_at=closed_at,
        )
        hotel_day_summary = await day_summary(hotel_day)
        utc_day_summary = await day_summary(utc_day)

    assert (hotel_day_summary.closed_total, hotel_day_summary.claim_median_seconds) == (1, 10 * 60)
    assert (utc_day_summary.closed_total, utc_day_summary.claim_median_seconds) == (0, None)


async def test_created_breakdown_covers_all_three_origins(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """issue #313: «создано» — знаменатель Exit-критерия целиком, а значений
    `origin` три. Сумма разбивки обязана сходиться с «создано»: пока ярлыков
    считалось два, заявка из публичной двери выпадала и из числителя, и из
    знаменателя разом — то есть переставала существовать для измерения."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a)
    category = await make_category(tenant_a)

    for origin in ServiceRequestOrigin:
        await make_request(tenant_a, category.id, summary=origin.value, origin=origin)

    with tenant_context(tenant_a):
        summary = await day_summary(hotel_today())

    assert summary.created_total == 3
    assert sum(summary.created_by_origin.values()) == summary.created_total
    assert summary.created_by_origin == dict.fromkeys(ServiceRequestOrigin, 1)


async def test_claim_median_counts_only_requests_claimed_that_day(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §6: медиана — по заявкам, ВЗЯТЫМ в этот день, а не созданным.
    Вчерашнее взятие с чудовищным ожиданием в сегодняшнее число не входит,
    иначе одна забытая заявка обесценила бы показатель целого дня."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a)
    category = await make_category(tenant_a)
    today = hotel_today()
    now = utc_now()

    waits_minutes = (1, 5, 60)
    for minutes in waits_minutes:
        request_id = await make_request(tenant_a, category.id, summary=f"взята через {minutes}")
        with tenant_context(tenant_a):
            await change_request_status(request_id, RequestStatus.IN_PROGRESS)
            await shift_request(
                request_id,
                created_at=now - timedelta(minutes=minutes),
                claimed_at=now,
            )
    stale_id = await make_request(tenant_a, category.id, summary="взята вчера")
    with tenant_context(tenant_a):
        await change_request_status(stale_id, RequestStatus.IN_PROGRESS)
        await shift_request(
            stale_id,
            created_at=hotel_noon(today - timedelta(days=1)) - timedelta(hours=10),
            claimed_at=hotel_noon(today - timedelta(days=1)),
        )
        summary = await day_summary(today)

    # Медиана трёх ожиданий 1/5/60 минут — 5 минут; среднее было бы 22.
    assert summary.claim_median_seconds == 5 * 60


async def test_hotel_without_reminders_shows_dash_not_zero(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §6/§9: у отеля с выключенными напоминаниями просрочки нет как
    явления — прочерк. Ноль читался бы как «сроки соблюдены»."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a, reminder_after_minutes=None)
    category = await make_category(tenant_a)
    request_id = await make_request(tenant_a, category.id, summary="висит невзятой")

    with tenant_context(tenant_a):
        await shift_request(request_id, created_at=utc_now() - timedelta(days=1))
        summary = await day_summary(hotel_today())

    assert summary.overdue is None
    assert summary.created_total == 1  # остальные числа считаются как обычно


async def test_unclaimed_cancellation_is_overdue_only_after_the_deadline(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §6: отсечка невзятой заявки — момент её закрытия. Отменённая в
    первые минуты до срока не доживает; отменённая через два часа при сроке 30
    минут просрочена и считается — это ровно тот случай, ради которого число
    считают: гость не дождался и отменил сам.

    День — фиксированная дата прошлого, метки — от полудня отеля. Сценарий,
    построенный от «сейчас», был бы зелёным лишь часть суток: пока локальное
    время отеля меньше 02:59, закрытие быстрой заявки («сейчас» минус три часа)
    приходилось на вчерашний день отеля и выпадало из «закрыто» — тест краснел
    бы на `closed_cancelled`, то есть не на том, что стережёт.

    Без отсечки по `closed_at` (`min(конец суток, сейчас, closed_at)`) снятая
    через минуту заявка досчитала бы себе полдня — до полуночи отеля — и попала
    в просрочку: тест обязан ловить именно это, а не разницу «минута против
    двух часов»."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a, reminder_after_minutes=30)
    category = await make_category(tenant_a)
    past_day = date(2026, 5, 17)
    created_at = hotel_noon(past_day)

    quick_id = await make_request(tenant_a, category.id, summary="отменили сразу")
    slow_id = await make_request(tenant_a, category.id, summary="гость не дождался")
    with tenant_context(tenant_a):
        await change_request_status(quick_id, RequestStatus.CANCELLED)
        await change_request_status(slow_id, RequestStatus.CANCELLED)
        await shift_request(
            quick_id,
            service_day=past_day,
            created_at=created_at,
            closed_at=created_at + timedelta(minutes=1),
        )
        await shift_request(
            slow_id,
            service_day=past_day,
            created_at=created_at,
            closed_at=created_at + timedelta(hours=2),
        )
        summary = await day_summary(past_day)

    assert summary.overdue == 1
    assert summary.closed_cancelled == 2  # обе отменены, но просрочена одна


async def test_overdue_of_a_past_day_stops_at_its_midnight(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §6: у прошедшего дня число просрочек уже не меняется. Заявку
    создали за десять минут до полуночи отеля при сроке 30 минут и не взяли
    вовсе — просрочку она получить не успела, и не получает её от того, что с
    тех пор прошли сутки.

    Второе слагаемое отсечки `min(конец суток отеля, сейчас, closed_at)` — здесь
    несущее: убери из неё конец суток, и невзятая заявка досчитает себе всё
    время до «сейчас», то есть месяцы.
    """
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a, reminder_after_minutes=30)
    category = await make_category(tenant_a)
    past_day = date(2026, 5, 17)

    request_id = await make_request(tenant_a, category.id, summary="создана под полночь")
    with tenant_context(tenant_a):
        await shift_request(
            request_id, service_day=past_day, created_at=hotel_moment(past_day, 23, 50)
        )
        summary = await day_summary(past_day)

    assert summary.overdue == 0
    assert summary.created_total == 1  # заявка в дне есть, просрочки в нём нет


async def test_open_now_does_not_depend_on_the_chosen_day(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §6: «открыто сейчас» — единственное число, которое смотрит на
    момент построения сводки, а не на день. При выборе «Вчера» оно то же."""
    tenant_a, _ = two_tenants
    await store_hotel_config(tenant_a)
    category = await make_category(tenant_a)
    await make_request(tenant_a, category.id, summary="висит открытой")
    today = hotel_today()

    with tenant_context(tenant_a):
        today_summary = await day_summary(today)
        yesterday_summary = await day_summary(today - timedelta(days=1))

    assert today_summary.open_now == 1
    assert yesterday_summary.open_now == 1
    assert yesterday_summary.created_total == 0


async def test_tenant_without_config_still_gets_numbers(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """spec 0035 §13: незавершённый онбординг (и служебный smoke-тенант) сводку
    не роняет — пояс деградирует на UTC, просрочка становится прочерком."""
    tenant_a, _ = two_tenants
    category = await make_category(tenant_a)
    await make_request(tenant_a, category.id, summary="без конфига")

    with tenant_context(tenant_a):
        summary = await day_summary(utc_now().date())

    assert summary.created_total == 1
    assert summary.overdue is None


async def test_two_tenants_do_not_see_each_others_numbers(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """P-4: сводка считается под RLS текущего тенанта. Числа соседнего отеля не
    попадают ни в одно из них — включая разрез по службам."""
    tenant_a, tenant_b = two_tenants
    await store_hotel_config(tenant_a)
    await store_hotel_config(tenant_b)
    category_a = await make_category(tenant_a)
    category_b = await make_category(tenant_b, key="fnb", name="Ресторан")
    await make_request(tenant_a, category_a.id, summary="первая A")
    await make_request(tenant_a, category_a.id, summary="вторая A")
    await make_request(tenant_b, category_b.id, summary="единственная B")
    today = hotel_today()

    with tenant_context(tenant_a):
        summary_a = await day_summary(today)
    with tenant_context(tenant_b):
        summary_b = await day_summary(today)

    assert summary_a.created_total == 2
    assert [row.category_id for row in summary_a.by_service] == [category_a.id]
    assert summary_b.created_total == 1
    assert [row.category_id for row in summary_b.by_service] == [category_b.id]
