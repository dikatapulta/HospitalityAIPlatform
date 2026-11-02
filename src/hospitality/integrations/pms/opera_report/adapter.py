"""Адаптер Opera: снимок из принятой выгрузки отчёта (spec 0058 §2.1, §2.4, ADR-004).

Отель раз в 5 минут шлёт файл отчёта «in-house» на маршрут загрузки
(`router.py`); адаптер разбирает его (`parse.py`) и хранит последний принятый
снимок тенанта в своей таблице `pms_report_feeds` — на диск файл не ложится.
Порт отдаёт этот снимок сверке (`fetch_snapshot`).

Каждый принятый файл — новый `source_id`, даже с теми же строками: сверка
считает выезд со второго снимка подряд без брони (§2.2), а снимок — это
выгрузка, а не изменение. Исключение — тот же файл байт в байт в пределах
`DUPLICATE_WINDOW`: это повтор задачи планировщика, а не второй снимок.

Второй файл — свободные номера по ночам (spec 0059 §2.1, `accept_availability`):
своя задача планировщика, свои колонки той же строки тенанта; повтор — по тому
же правилу. Порт отдаёт его боту о продлении (`fetch_availability`).

Токен загрузки — на тенанта, в БД только SHA-256 (ADR-008); plaintext
печатает один раз выпуск (`python -m hospitality.tools.pms_token`).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final

from sqlalchemy import select

from hospitality.integrations.pms.opera_report.models import PmsReportFeed
from hospitality.integrations.pms.opera_report.parse import parse_availability, parse_report
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.ports.pms import (
    PmsAvailability,
    PmsNightAvailability,
    PmsSnapshot,
    PmsStayRow,
)
from hospitality.shared.tenancy import tenant_context

# Тот же файл в пределах минуты — повтор задачи Windows, а не второй снимок (§2.4).
DUPLICATE_WINDOW: Final = timedelta(seconds=60)


@dataclass(frozen=True)
class UploadResult:
    """Итог приёма файла: `accepted=False` — повтор, прежний файл не менялся.

    `rows` — строк данных в файле: броней у снимка, ночей у свободных номеров.
    """

    accepted: bool
    rows: int


class UploadFeedMissingError(LookupError):
    """У тенанта нет строки выгрузки — токен не выпускался (маршрут сюда не пустит)."""


def hash_upload_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_upload_token(tenant_id: uuid.UUID) -> str:
    """Выпустить токен загрузки тенанта; прежний гаснет. Вернуть plaintext — один раз.

    Снимок, если он был, остаётся: перевыпуск меняет только ключ двери.
    """
    token = secrets.token_urlsafe(32)
    with tenant_context(tenant_id):
        async with session_scope() as session:
            feed = await session.get(PmsReportFeed, tenant_id, with_for_update=True)
            if feed is None:
                session.add(PmsReportFeed(tenant_id=tenant_id, token_hash=hash_upload_token(token)))
            else:
                feed.token_hash = hash_upload_token(token)
    return token


async def upload_token_matches(tenant_id: uuid.UUID, token: str) -> bool:
    """Подходит ли токен тенанту; токен не выпускался — False."""
    with tenant_context(tenant_id):
        async with session_scope() as session:
            stored = await session.scalar(
                select(PmsReportFeed.token_hash).where(PmsReportFeed.tenant_id == tenant_id)
            )
    # Сравнение хэшей за постоянное время: обычное `==` утекает длиной совпавшего префикса.
    return stored is not None and secrets.compare_digest(stored, hash_upload_token(token))


class OperaReportPmsAdapter:
    backend = "opera_report"

    def __init__(self, *, max_guests: int) -> None:
        # Потолок гостей одного Stay знает домен; адаптеру его передаёт composition root.
        self._max_guests = max_guests

    async def fetch_snapshot(self, tenant_id: uuid.UUID) -> PmsSnapshot | None:
        with tenant_context(tenant_id):
            async with session_scope() as session:
                feed = await session.get(PmsReportFeed, tenant_id)
        if feed is None or feed.source_id is None or feed.taken_at is None:
            return None
        return PmsSnapshot(
            source_id=feed.source_id,
            taken_at=feed.taken_at,
            rows=tuple(_row_from_json(row) for row in feed.rows),
        )

    async def fetch_availability(self, tenant_id: uuid.UUID) -> PmsAvailability | None:
        with tenant_context(tenant_id):
            async with session_scope() as session:
                feed = await session.get(PmsReportFeed, tenant_id)
        if feed is None or feed.availability_taken_at is None:
            return None
        return PmsAvailability(
            taken_at=feed.availability_taken_at,
            nights=tuple(_night_from_json(night) for night in feed.availability_nights),
        )

    async def check(self) -> bool:
        # Источник — таблица в той же БД: её готовность проверяет `/health/ready`,
        # опоздавший файл — алерт ERR-GUESTS-010 (spec 0058 §2.6).
        return True

    async def accept_upload(
        self, tenant_id: uuid.UUID, body: bytes, *, now: datetime | None = None
    ) -> UploadResult:
        """Принять файл: разбор, повтор, новый снимок. Не по контракту — `ReportRejectedError`.

        Отказ разбора снимок не меняет: прежний остаётся в силе.
        """
        rows = parse_report(body, max_guests=self._max_guests)
        digest = hashlib.sha256(body).hexdigest()
        moment = now if now is not None else utc_now()
        with tenant_context(tenant_id):
            async with session_scope() as session:
                # FOR UPDATE: два одновременных файла не примут один повтор дважды.
                feed = await session.get(PmsReportFeed, tenant_id, with_for_update=True)
                if feed is None:
                    raise UploadFeedMissingError(str(tenant_id))
                if (
                    feed.content_sha256 == digest
                    and feed.taken_at is not None
                    and moment - feed.taken_at < DUPLICATE_WINDOW
                ):
                    return UploadResult(accepted=False, rows=len(rows))
                feed.source_id = uuid.uuid4()
                feed.taken_at = moment
                feed.content_sha256 = digest
                feed.rows = [_row_to_json(row) for row in rows]
        return UploadResult(accepted=True, rows=len(rows))

    async def accept_availability(
        self, tenant_id: uuid.UUID, body: bytes, *, now: datetime | None = None
    ) -> UploadResult:
        """Принять файл свободных номеров: разбор, повтор, замена целиком.

        Не по контракту — `ReportRejectedError`, прежний файл остаётся в силе.
        Снимок «кто в доме» не трогает: у файлов свои колонки строки тенанта.
        """
        nights = parse_availability(body)
        digest = hashlib.sha256(body).hexdigest()
        moment = now if now is not None else utc_now()
        with tenant_context(tenant_id):
            async with session_scope() as session:
                feed = await session.get(PmsReportFeed, tenant_id, with_for_update=True)
                if feed is None:
                    raise UploadFeedMissingError(str(tenant_id))
                if (
                    feed.availability_sha256 == digest
                    and feed.availability_taken_at is not None
                    and moment - feed.availability_taken_at < DUPLICATE_WINDOW
                ):
                    return UploadResult(accepted=False, rows=len(nights))
                feed.availability_taken_at = moment
                feed.availability_sha256 = digest
                feed.availability_nights = [
                    {"night": night.night.isoformat(), "free_rooms": night.free_rooms}
                    for night in nights
                ]
        return UploadResult(accepted=True, rows=len(nights))


def _night_from_json(night: dict[str, Any]) -> PmsNightAvailability:
    return PmsNightAvailability(
        night=date.fromisoformat(night["night"]), free_rooms=night["free_rooms"]
    )


def _row_to_json(row: PmsStayRow) -> dict[str, Any]:
    return {
        "room": row.room_number,
        "booking_ref": row.booking_ref,
        "arrival": row.arrival.isoformat(),
        "departure": row.departure.isoformat(),
        "guests": row.guests_count,
    }


def _row_from_json(row: dict[str, Any]) -> PmsStayRow:
    return PmsStayRow(
        room_number=row["room"],
        booking_ref=row["booking_ref"],
        arrival=date.fromisoformat(row["arrival"]),
        departure=date.fromisoformat(row["departure"]),
        guests_count=row["guests"],
    )
