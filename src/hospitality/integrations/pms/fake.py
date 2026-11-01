"""Fake-адаптер PMS (ADR-007, spec 0058 §2.1): «Opera» в памяти процесса.

Обычный адаптер порта `PmsPort`: тот же контракт и те же контрактные тесты,
что у адаптера Opera (PR 4 серии #122). У тенанта две вещи — «дом сейчас»
(брони, которые тесты и dev двигают методами ниже) и последний выпущенный
снимок. Правки дома в снимок не попадают, пока не вызван `publish`: так
сверка видит то же, что видела бы при выгрузке раз в 5 минут.

Для тестов, dev и демо; на staging и копии сверки по нему нет — у тенанта
там `pms_source: manual`, а память у каждого процесса своя.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date, datetime

from hospitality.shared.db import utc_now
from hospitality.shared.ports.pms import PmsSnapshot, PmsStayRow


class FakePmsAdapter:
    backend = "fake"

    def __init__(self) -> None:
        self._house: dict[uuid.UUID, dict[str, PmsStayRow]] = {}
        self._published: dict[uuid.UUID, PmsSnapshot] = {}

    async def fetch_snapshot(self, tenant_id: uuid.UUID) -> PmsSnapshot | None:
        return self._published.get(tenant_id)

    async def check(self) -> bool:
        return True

    # --- Дом отеля: правки до следующего `publish` -------------------------

    def check_in_room(
        self,
        tenant_id: uuid.UUID,
        room_number: str,
        booking_ref: str,
        *,
        arrival: date,
        departure: date,
        guests_count: int | None = None,
    ) -> None:
        """Заселить бронь в комнату; бронь уже в доме — `ValueError` (опечатка теста)."""
        house = self._house.setdefault(tenant_id, {})
        if booking_ref in house:
            raise ValueError(f"booking {booking_ref!r} is already in the house")
        house[booking_ref] = PmsStayRow(
            room_number=room_number,
            booking_ref=booking_ref,
            arrival=arrival,
            departure=departure,
            guests_count=guests_count,
        )

    def check_out_booking(self, tenant_id: uuid.UUID, booking_ref: str) -> None:
        """Выезд брони: её строки в следующем снимке нет."""
        self._row(tenant_id, booking_ref)
        del self._house[tenant_id][booking_ref]

    def move_booking(self, tenant_id: uuid.UUID, booking_ref: str, room_number: str) -> None:
        """Переселение брони в другую комнату."""
        row = self._row(tenant_id, booking_ref)
        self._house[tenant_id][booking_ref] = dataclasses.replace(row, room_number=room_number)

    def set_departure(self, tenant_id: uuid.UUID, booking_ref: str, departure: date) -> None:
        """Новая дата выезда брони (продление или ранний выезд)."""
        row = self._row(tenant_id, booking_ref)
        self._house[tenant_id][booking_ref] = dataclasses.replace(row, departure=departure)

    def publish(self, tenant_id: uuid.UUID, *, taken_at: datetime | None = None) -> PmsSnapshot:
        """Выпустить снимок дома: новый `source_id`, `taken_at` — сейчас или заданный тестом."""
        rows = sorted(
            self._house.get(tenant_id, {}).values(),
            key=lambda row: (row.room_number, row.booking_ref),
        )
        snapshot = PmsSnapshot(
            source_id=uuid.uuid4(),
            taken_at=taken_at if taken_at is not None else utc_now(),
            rows=tuple(rows),
        )
        self._published[tenant_id] = snapshot
        return snapshot

    def _row(self, tenant_id: uuid.UUID, booking_ref: str) -> PmsStayRow:
        try:
            return self._house[tenant_id][booking_ref]
        except KeyError:
            raise KeyError(f"booking {booking_ref!r} is not in the house") from None
