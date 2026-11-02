"""Порт PMS: снимок проживаний и свободные номера отеля из его PMS (spec 0058 §2.1, spec 0059 §2.2).

Канон порта — `shared/ports/file_storage.py`: `typing.Protocol` здесь, адаптеры —
`integrations/pms/` (Fake обязателен, ADR-007), общий контрактный набор тестов
(`tests/test_pms_port.py`), адаптер в порт кладёт только composition root (`set_pms`).

Порт отдаёт последний принятый снимок тенанта — кто в какой комнате, по какой
брони и до какого дня — и ничего не знает о Stay, кодах и сверке: это работа
`modules/guests` (spec 0058 §2.2). Имён гостей в снимке нет (ADR-004, состав
выгрузки — решение основателя 06.08).

Второй файл того же отеля — свободные номера по ночам (`PmsAvailability`,
spec 0059): ни броней, ни комнат, ни гостей, только число на ночь. Его читает
`modules/guests`, чтобы бот знал, есть ли в отеле места на продление.

Строки и снимок проверяют себя при создании: адаптер не может отдать ядру
строку, которая не ляжет в колонки `stays` (длины — те же, что у колонок), бронь
с выездом раньше заезда, снимок с одной бронью дважды или ночи вразнобой.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Final, Protocol

# Длины — колонки `stays.room_number` (20) и `stays.pms_booking_ref` (64, spec 0058 §3).
MAX_ROOM_NUMBER_LENGTH: Final = 20
MAX_BOOKING_REF_LENGTH: Final = 64


@dataclass(frozen=True)
class PmsStayRow:
    """Одна бронь в доме: комната, номер брони, даты по календарю отеля, число гостей."""

    room_number: str
    booking_ref: str
    arrival: date
    departure: date
    # `None` — в выгрузке нет колонки гостей; верхний предел (`MAX_GUESTS_PER_STAY`)
    # знает домен, его проверяет адаптер при разборе (spec 0058 §2.4).
    guests_count: int | None = None

    def __post_init__(self) -> None:
        if not self.room_number.strip() or len(self.room_number) > MAX_ROOM_NUMBER_LENGTH:
            raise ValueError("room_number must be 1..20 non-blank characters")
        if not self.booking_ref.strip() or len(self.booking_ref) > MAX_BOOKING_REF_LENGTH:
            raise ValueError("booking_ref must be 1..64 non-blank characters")
        if self.departure < self.arrival:
            raise ValueError("departure is before arrival")
        if self.guests_count is not None and self.guests_count < 1:
            raise ValueError("guests_count must be positive")


@dataclass(frozen=True)
class PmsSnapshot:
    """Снимок дома на момент `taken_at` (UTC); `source_id` — у каждого снимка свой.

    Повтор того же `source_id` значит «нового снимка не было» — сверка его
    пропускает (ADR-004 п. 6).
    """

    source_id: uuid.UUID
    taken_at: datetime
    rows: tuple[PmsStayRow, ...]

    def __post_init__(self) -> None:
        if self.taken_at.utcoffset() != timedelta(0):
            raise ValueError("taken_at must be an aware UTC datetime")
        refs = [row.booking_ref for row in self.rows]
        if len(refs) != len(set(refs)):
            raise ValueError("booking_ref repeats within one snapshot")


@dataclass(frozen=True)
class PmsNightAvailability:
    """Ночь с `night` на следующий день по календарю отеля: сколько номеров свободно.

    `free_rooms` ≤ 0 — мест нет (у Opera с овербукингом число бывает отрицательным).
    """

    night: date
    free_rooms: int


@dataclass(frozen=True)
class PmsAvailability:
    """Свободные номера по ночам, принятые в `taken_at` (UTC); ночи — подряд, по возрастанию."""

    taken_at: datetime
    nights: tuple[PmsNightAvailability, ...]

    def __post_init__(self) -> None:
        if self.taken_at.utcoffset() != timedelta(0):
            raise ValueError("taken_at must be an aware UTC datetime")
        for previous, current in pairwise(self.nights):
            if current.night - previous.night != timedelta(days=1):
                raise ValueError("nights must be consecutive, ascending and without repeats")


class PmsNotConfiguredError(RuntimeError):
    """Composition root не положил адаптер — программная ошибка сборки процесса."""


class PmsPort(Protocol):
    """Источник снимков проживаний и свободных номеров.

    Контракт (общие тесты — `tests/test_pms_port.py`):
    - `fetch_snapshot` — последний принятый снимок тенанта; `None` — снимков ещё
      не было; повторный вызов без нового снимка отдаёт тот же `source_id` (P-8);
    - `fetch_availability` — последний принятый файл свободных номеров; `None` —
      файлов ещё не было; со снимком не связан: один приходит без другого;
    - снимки и файлы тенантов независимы: данные одного отеля не видны другому;
    - `check` — health адаптера (§8 правило 1): `True`, если источник готов.
    """

    backend: str

    async def fetch_snapshot(self, tenant_id: uuid.UUID) -> PmsSnapshot | None: ...

    async def fetch_availability(self, tenant_id: uuid.UUID) -> PmsAvailability | None: ...

    async def check(self) -> bool: ...


_pms: PmsPort | None = None


def set_pms(pms: PmsPort) -> None:
    """Положить адаптер процесса. Зовёт только composition root (и тесты)."""
    global _pms
    _pms = pms


def get_pms() -> PmsPort:
    """Адаптер процесса; не положен — `PmsNotConfiguredError`."""
    if _pms is None:
        raise PmsNotConfiguredError("PMS is not configured: composition root must call set_pms()")
    return _pms


def pms_or_none() -> PmsPort | None:
    """То же без исключения — для тестов, возвращающих прежний адаптер на место."""
    return _pms
