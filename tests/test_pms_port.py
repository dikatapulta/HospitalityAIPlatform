"""Контрактные тесты порта PMS (R-7, ADR-007, spec 0058 §2.1).

Канон — `tests/test_file_storage_port.py`: один набор на все адаптеры. Порт
только читает, поэтому у каждого адаптера своя «выгрузка» — `_Harness.publish`
кладёт снимок тем путём, каким его кладёт отель (у Fake — методы дома, у
адаптера Opera — файл отчёта через приём адаптера; маршрут поверх него —
`test_pms_opera_report.py`). Новый адаптер — строкой в `ADAPTERS`: не прошёл
контракт — дефект адаптера.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Protocol

import pytest

from hospitality.integrations.pms import (
    FakePmsAdapter,
    OperaReportPmsAdapter,
    build_pms,
    issue_upload_token,
)
from hospitality.platform.models import Tenant
from hospitality.shared.config import Settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.ports import pms as pms_port
from hospitality.shared.ports.pms import (
    PmsNotConfiguredError,
    PmsPort,
    PmsSnapshot,
    PmsStayRow,
    get_pms,
    set_pms,
)

_HOTEL = uuid.UUID("11111111-2222-3333-4444-555555555555")
_OTHER_HOTEL = uuid.UUID("66666666-7777-8888-9999-000000000000")
_ARRIVAL = date(2026, 11, 2)


def _row(room: str, booking: str, nights: int = 2, guests: int | None = 2) -> PmsStayRow:
    return PmsStayRow(
        room_number=room,
        booking_ref=booking,
        arrival=_ARRIVAL,
        departure=_ARRIVAL + timedelta(days=nights),
        guests_count=guests,
    )


class _Harness(Protocol):
    """Адаптер в наборе: порт и «выгрузка» — снимок тем путём, каким его кладёт отель."""

    port: PmsPort

    async def publish(self, tenant_id: uuid.UUID, rows: Sequence[PmsStayRow]) -> None: ...


class _FakeHarness:
    """Выгрузка в Fake: дом приводится к строкам снимка, затем `publish`."""

    def __init__(self) -> None:
        self.adapter = FakePmsAdapter()
        self.port: PmsPort = self.adapter
        self._in_house: dict[uuid.UUID, set[str]] = {}

    async def publish(self, tenant_id: uuid.UUID, rows: Sequence[PmsStayRow]) -> None:
        for booking in self._in_house.pop(tenant_id, set()):
            self.adapter.check_out_booking(tenant_id, booking)
        for row in rows:
            self.adapter.check_in_room(
                tenant_id,
                row.room_number,
                row.booking_ref,
                arrival=row.arrival,
                departure=row.departure,
                guests_count=row.guests_count,
            )
        self._in_house[tenant_id] = {row.booking_ref for row in rows}
        self.adapter.publish(tenant_id)


def _report_file(rows: Sequence[PmsStayRow]) -> bytes:
    """Файл отчёта Opera по контракту spec 0058 §2.4: заголовок, `,`, ISO-даты."""
    lines = ["room,confirmation_no,arrival,departure,guests"]
    for row in rows:
        guests = "" if row.guests_count is None else str(row.guests_count)
        lines.append(f"{row.room_number},{row.booking_ref},{row.arrival},{row.departure},{guests}")
    return ("\n".join(lines) + "\n").encode()


class _OperaHarness:
    """Выгрузка в адаптер Opera: отель (тенант в БД, выпущенный токен) шлёт файл.

    Часы двигаются на 5 минут на файл — как задача планировщика: тот же файл
    в пределах минуты адаптер считает повтором, а не снимком (§2.4).
    """

    def __init__(self) -> None:
        self.adapter = OperaReportPmsAdapter(max_guests=20)
        self.port: PmsPort = self.adapter
        self._clock = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
        self._hotels: set[uuid.UUID] = set()

    async def publish(self, tenant_id: uuid.UUID, rows: Sequence[PmsStayRow]) -> None:
        if tenant_id not in self._hotels:
            async with platform_session_scope() as session:
                session.add(Tenant(id=tenant_id, slug=f"hotel-{tenant_id.hex[:8]}", name="Hotel"))
            await issue_upload_token(tenant_id)
            self._hotels.add(tenant_id)
        self._clock += timedelta(minutes=5)
        await self.adapter.accept_upload(tenant_id, _report_file(rows), now=self._clock)


ADAPTERS: dict[str, Callable[[], _Harness]] = {
    "fake": _FakeHarness,
    "opera_report": _OperaHarness,
}
# Адаптеры, чей снимок живёт в БД: набору нужна временная база на `head`.
_NEEDS_DATABASE = frozenset({"opera_report"})


@pytest.fixture(params=sorted(ADAPTERS))
def harness(request: pytest.FixtureRequest) -> _Harness:
    if request.param in _NEEDS_DATABASE:
        request.getfixturevalue("canonical_database")
    return ADAPTERS[request.param]()


async def _fetch(harness: _Harness, tenant_id: uuid.UUID = _HOTEL) -> PmsSnapshot:
    snapshot = await harness.port.fetch_snapshot(tenant_id)
    assert snapshot is not None
    return snapshot


async def test_no_snapshot_yet_is_none(harness: _Harness) -> None:
    assert await harness.port.fetch_snapshot(_HOTEL) is None


async def test_published_rows_come_back(harness: _Harness) -> None:
    rows = [_row("305", "123456"), _row("410", "654321", nights=5, guests=None)]
    await harness.publish(_HOTEL, rows)

    snapshot = await _fetch(harness)

    assert set(snapshot.rows) == set(rows)
    assert snapshot.taken_at.utcoffset() == timedelta(0)


async def test_fetch_without_new_snapshot_keeps_source_id(harness: _Harness) -> None:
    await harness.publish(_HOTEL, [_row("305", "123456")])
    first = await _fetch(harness)
    assert (await _fetch(harness)).source_id == first.source_id


async def test_every_snapshot_has_its_own_source_id(harness: _Harness) -> None:
    rows = [_row("305", "123456")]
    await harness.publish(_HOTEL, rows)
    first = await _fetch(harness)
    await harness.publish(_HOTEL, rows)
    second = await _fetch(harness)

    assert second.source_id != first.source_id
    assert second.rows == first.rows


async def test_empty_house_is_a_snapshot_not_none(harness: _Harness) -> None:
    await harness.publish(_HOTEL, [_row("305", "123456")])
    await harness.publish(_HOTEL, [])
    assert (await _fetch(harness)).rows == ()


async def test_tenants_do_not_see_each_other(harness: _Harness) -> None:
    await harness.publish(_HOTEL, [_row("305", "123456")])

    assert await harness.port.fetch_snapshot(_OTHER_HOTEL) is None
    await harness.publish(_OTHER_HOTEL, [_row("101", "777777")])
    assert {row.booking_ref for row in (await _fetch(harness)).rows} == {"123456"}


async def test_check_reports_ready(harness: _Harness) -> None:
    assert await harness.port.check() is True


# --- Строка и снимок проверяют себя ------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"departure": _ARRIVAL - timedelta(days=1)}, "departure is before arrival"),
        ({"room_number": "  "}, "room_number"),
        ({"room_number": "1" * 21}, "room_number"),
        ({"booking_ref": ""}, "booking_ref"),
        ({"booking_ref": "9" * 65}, "booking_ref"),
        ({"guests_count": 0}, "guests_count"),
    ],
)
def test_row_rejects_what_stays_cannot_hold(changes: dict[str, object], reason: str) -> None:
    fields: dict[str, object] = {
        "room_number": "305",
        "booking_ref": "123456",
        "arrival": _ARRIVAL,
        "departure": _ARRIVAL + timedelta(days=1),
        "guests_count": 2,
    }
    with pytest.raises(ValueError, match=reason):
        PmsStayRow(**{**fields, **changes})  # type: ignore[arg-type]


def test_same_day_departure_is_a_valid_row() -> None:
    assert _row("305", "123456", nights=0).departure == _ARRIVAL


@pytest.mark.parametrize(
    "taken_at",
    [
        datetime(2026, 11, 2, 9, 0),  # noqa: DTZ001 — наивное время и есть суть теста
        datetime(2026, 11, 2, 9, 0, tzinfo=timezone(timedelta(hours=5))),
    ],
)
def test_snapshot_time_must_be_utc(taken_at: datetime) -> None:
    with pytest.raises(ValueError, match="UTC"):
        PmsSnapshot(source_id=uuid.uuid4(), taken_at=taken_at, rows=())


def test_snapshot_rejects_repeated_booking() -> None:
    with pytest.raises(ValueError, match="repeats"):
        PmsSnapshot(
            source_id=uuid.uuid4(),
            taken_at=datetime(2026, 11, 2, 9, 0, tzinfo=UTC),
            rows=(_row("305", "123456"), _row("410", "123456")),
        )


# --- Fake сверх контракта: дом отеля двигают тесты и dev -------------------


async def test_fake_house_changes_wait_for_publish() -> None:
    pms = FakePmsAdapter()
    pms.check_in_room(_HOTEL, "305", "123456", arrival=_ARRIVAL, departure=date(2026, 11, 4))
    assert await pms.fetch_snapshot(_HOTEL) is None

    published = pms.publish(_HOTEL)
    pms.check_out_booking(_HOTEL, "123456")

    assert await pms.fetch_snapshot(_HOTEL) == published
    assert [row.booking_ref for row in published.rows] == ["123456"]


async def test_fake_moves_extends_and_checks_out() -> None:
    pms = FakePmsAdapter()
    pms.check_in_room(_HOTEL, "305", "1", arrival=_ARRIVAL, departure=date(2026, 11, 4))
    pms.check_in_room(_HOTEL, "410", "2", arrival=_ARRIVAL, departure=date(2026, 11, 3))
    pms.check_in_room(_HOTEL, "512", "3", arrival=_ARRIVAL, departure=date(2026, 11, 5))

    pms.move_booking(_HOTEL, "1", "306")
    pms.set_departure(_HOTEL, "2", date(2026, 11, 6))
    pms.check_out_booking(_HOTEL, "3")
    snapshot = pms.publish(_HOTEL)

    assert [(row.room_number, row.booking_ref, row.departure) for row in snapshot.rows] == [
        ("306", "1", date(2026, 11, 4)),
        ("410", "2", date(2026, 11, 6)),
    ]


def test_fake_two_bookings_share_a_room() -> None:
    pms = FakePmsAdapter()
    pms.check_in_room(_HOTEL, "305", "1", arrival=_ARRIVAL, departure=date(2026, 11, 4))
    pms.check_in_room(_HOTEL, "305", "2", arrival=_ARRIVAL, departure=date(2026, 11, 5))
    assert [row.booking_ref for row in pms.publish(_HOTEL).rows] == ["1", "2"]


def test_fake_catches_typos_in_tests() -> None:
    pms = FakePmsAdapter()
    pms.check_in_room(_HOTEL, "305", "1", arrival=_ARRIVAL, departure=date(2026, 11, 4))
    with pytest.raises(ValueError, match="already in the house"):
        pms.check_in_room(_HOTEL, "306", "1", arrival=_ARRIVAL, departure=date(2026, 11, 4))
    with pytest.raises(KeyError):
        pms.move_booking(_HOTEL, "404", "306")
    with pytest.raises(KeyError):
        pms.check_out_booking(_OTHER_HOTEL, "1")


def test_fake_publish_keeps_given_time() -> None:
    moment = datetime(2026, 11, 2, 9, 5, tzinfo=UTC)
    assert FakePmsAdapter().publish(_HOTEL, taken_at=moment).taken_at == moment


# --- Composition root -------------------------------------------------------


def test_port_without_adapter_is_a_build_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pms_port, "_pms", None)
    with pytest.raises(PmsNotConfiguredError):
        get_pms()
    set_pms(build_pms(Settings(), max_guests=20))
    assert get_pms().backend == "fake"


def test_backend_setting_picks_opera_adapter() -> None:
    assert build_pms(Settings(pms_backend="opera_report"), max_guests=20).backend == "opera_report"
