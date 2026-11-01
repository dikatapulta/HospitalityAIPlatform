"""Сверка со снимком PMS: повтор, обрыв, защита, гонка, изоляция, алерты (spec 0058 §5 п. 2–4).

Фикстуры и шаги «дома отеля» — из `test_pms_sync.py`; F811 отключён на файл:
параметр-фикстура обязан называться как реимпортированная фикстура.
"""

# ruff: noqa: F811

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from hospitality.integrations.pms import FakePmsAdapter
from hospitality.modules.guests import api as guests_api
from hospitality.modules.guests import pms_apply, pms_sync
from hospitality.modules.guests.models import Stay, StaySource
from hospitality.modules.guests.pms_plan import HouseStay, plan_sync
from hospitality.modules.guests.service import close_stay
from hospitality.platform.models import StaffAction
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.ports.pms import PmsStayRow
from hospitality.shared.tenancy import tenant_context
from hospitality.worker import run_worker
from tests.test_pms_sync import (  # noqa: F401  (фикстуры для pytest)
    ZONE,
    active,
    add_hotel,
    book,
    hotel,
    journal,
    pms,
    state,
    stays,
    sync,
    today,
)
from tests.test_tenant_config import _wait_until_a_backend_blocks


@pytest.fixture
def fast_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Готовый bcrypt-хэш вместо нового на каждый заезд: десятки заселений за доли секунды."""
    fixed = "$2b$04$" + "a" * 53

    async def instant_hash() -> str:
        return fixed

    monkeypatch.setattr(pms_apply, "new_access_code_hash", instant_hash)


async def test_same_snapshot_is_applied_once(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Повтор того же `source_id` — ноль действий (ADR-004 п. 6)."""
    book(pms, hotel, "305", "123456")
    assert await sync(pms, hotel) == 1
    pms.check_out_booking(hotel, "123456")  # правка дома без выгрузки снимок не меняет
    assert await guests_api.sync_stays_from_pms() == 0
    assert (await active(hotel))["305"].pms_missed_snapshots == 0


async def test_run_broken_after_check_outs_does_not_count_a_miss_twice(
    pms: FakePmsAdapter, hotel: uuid.UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прогон оборвался после выездов: повтор того же снимка закрывает только своё, а
    пропуск второй брони считается один раз — счётчики пишутся в конце (§2.2)."""
    book(pms, hotel, "305", "111111")
    book(pms, hotel, "306", "222222")
    await sync(pms, hotel)
    pms.check_out_booking(hotel, "111111")
    await sync(pms, hotel)  # первый пропуск 111111
    pms.check_out_booking(hotel, "222222")
    pms.publish(hotel)

    finish = pms_sync._finish

    async def broken_finish(*args: object, **kwargs: object) -> None:
        raise RuntimeError("db went away")

    monkeypatch.setattr(pms_sync, "_finish", broken_finish)
    assert await guests_api.sync_stays_from_pms() == 0  # шаг выезда был, конца — нет
    assert "305" not in await active(hotel)

    monkeypatch.setattr(pms_sync, "_finish", finish)
    assert await guests_api.sync_stays_from_pms() == 0
    assert (await active(hotel))["306"].pms_missed_snapshots == 1
    assert await guests_api.sync_stays_from_pms() == 0  # снимок уже применён


async def test_mass_disappearance_holds_check_outs_and_alerts_once(
    pms: FakePmsAdapter, hotel: uuid.UUID, fast_codes: None
) -> None:
    """11 из 50 пропали разом — выезды придержаны, счётчики не растут; заезд применён;
    алерт ERR-GUESTS-009 — один на эпизод (§2.3)."""
    for index in range(50):
        book(pms, hotel, str(1000 + index), str(500000 + index))
    await sync(pms, hotel)
    for index in range(11):
        pms.check_out_booking(hotel, str(500000 + index))
    book(pms, hotel, "2000", "600000")

    alerts: list[str] = []

    async def send(text: str) -> None:
        alerts.append(text)

    for _ in range(2):
        pms.publish(hotel)
        await guests_api.sync_stays_from_pms(alert_sender=send)

    house = await active(hotel)
    assert len(house) == 51
    assert all(stay.pms_missed_snapshots == 0 for stay in house.values())
    assert (await state(hotel)).held_closures == 11
    assert len(alerts) == 1
    assert "ERR-GUESTS-009" in alerts[0]
    assert "11 из 50" in alerts[0]


def test_ten_of_thirty_is_a_normal_departure() -> None:
    """10 из 30 — не больше десяти: выезды идут обычным порядком (граница §2.3)."""
    now = utc_now()
    house = [
        HouseStay(uuid.uuid4(), str(100 + i), StaySource.PMS, str(i), now, now, 1)
        for i in range(30)
    ]
    rows = [
        PmsStayRow(str(100 + i), str(i), today(), today() + timedelta(days=1))
        for i in range(10, 30)
    ]
    plan = plan_sync(rows, house, frozenset(), zone=ZONE, checkout_hour=12, now=now)
    assert (plan.held_closures, len(plan.check_outs)) == (0, 10)


async def test_sync_waits_for_staff_holding_the_stay_and_sees_the_result(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Сотрудник держит Stay `FOR UPDATE` и выписывает его: выезд сверки ждёт, видит
    закрытый Stay и второй раз его не закрывает (§5 п. 4)."""
    book(pms, hotel, "305", "123456")
    await sync(pms, hotel)
    pms.check_out_booking(hotel, "123456")
    await sync(pms, hotel)
    pms.publish(hotel)
    stay_id = (await active(hotel))["305"].id

    with tenant_context(hotel):
        async with session_scope() as session:
            stay = await session.scalar(select(Stay).where(Stay.id == stay_id).with_for_update())
            assert stay is not None
            run = asyncio.create_task(guests_api.sync_stays_from_pms())
            assert await _wait_until_a_backend_blocks()
            await close_stay(session, stay, utc_now())
    assert await run == 0
    actions = [row.action for row in await journal(hotel)]
    assert StaffAction.STAY_PMS_CHECKED_OUT not in actions


async def test_other_hotels_are_untouched(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    """Снимок одного отеля не трогает другой; отель без сверки (`manual`) снимок не читает."""
    manual_hotel = await add_hotel("hotel-manual", pms_source="manual")
    book(pms, manual_hotel, "305", "999999")
    pms.publish(manual_hotel)
    book(pms, hotel, "305", "123456")
    await sync(pms, hotel)
    assert await stays(manual_hotel) == []
    assert (await active(hotel))["305"].pms_booking_ref == "123456"


async def test_stale_snapshot_alerts_once_and_recovery_is_reported(
    pms: FakePmsAdapter, hotel: uuid.UUID
) -> None:
    """Снимок старше трёх интервалов — ERR-GUESTS-010 один раз; свежий — «снова приходит» (§2.6)."""
    alerts: list[str] = []

    async def send(text: str) -> None:
        alerts.append(text)

    pms.publish(hotel, taken_at=utc_now() - timedelta(minutes=16))
    for _ in range(2):
        await guests_api.sync_stays_from_pms(alert_sender=send)
    assert len(alerts) == 1
    assert alerts[0].startswith("🔴") and "ERR-GUESTS-010" in alerts[0]

    pms.publish(hotel)
    await guests_api.sync_stays_from_pms(alert_sender=send)
    assert len(alerts) == 2
    assert alerts[1].startswith("✅")
    assert (await state(hotel)).stale_alerted_at is None


async def test_snapshot_age_metric_per_opera_hotel(pms: FakePmsAdapter, hotel: uuid.UUID) -> None:
    pms.publish(hotel, taken_at=datetime.now(UTC) - timedelta(minutes=2))
    await guests_api.refresh_pms_snapshot_age_metrics()
    age = REGISTRY.get_sample_value("pms_snapshot_age_seconds", {"tenant_id": str(hotel)})
    assert age is not None and 110 < age < 300


async def test_worker_runs_pms_sync_on_start_and_survives_its_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Шаг цикла воркера (spec 0058 §2.2): стартовый прогон и дальше по интервалу;
    сбой прогона — ERR-GUESTS-008 в лог, цикл живёт."""
    calls = 0

    async def broken_sync(*, alert_sender: object = None) -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("db is down")

    async def empty_delivery(*args: object, **kwargs: object) -> int:
        return 0

    monkeypatch.setattr("hospitality.worker.guests_api.sync_stays_from_pms", broken_sync)
    monkeypatch.setattr("hospitality.worker.deliver_pending_events", empty_delivery)
    monkeypatch.setenv("WORKER_PMS_SYNC_INTERVAL_SECONDS", "3600")
    monkeypatch.setenv("WORKER_POLL_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    try:
        await run_worker(iterations=3)  # не бросает — иначе тест упал бы здесь
    finally:
        get_settings.cache_clear()
    assert calls == 1  # только стартовый; итерации 2–3 внутри интервала
