"""Миграция 0025 — бэкфилл измеримости заявок (spec 0035 §3–§4, issue #298).

Проверяется поведением, а не формой файла: «доисторическая» БД поднимается до
ревизии `0024`, наполняется строками сырым SQL (как их видел бы живой отель на
момент выкатки), и только потом делается шаг на `0025`. Так тест ловит ровно
то, ради чего миграция написана, — что стало со СТАРЫМИ строками:

- `origin` у них обязан стать `guest_chat` (единственный путь создания,
  существовавший до этой спеки), причём именно у всех: NOT NULL без умолчания
  после бэкфилла означает, что пропущенная строка уронила бы саму миграцию;
- `closed_at` терминальных обязан стать `updated_at` — и НЕ обязан у уже
  обезличенных: у них `updated_at` — момент прогона ретеншна (#42), а не
  закрытия (дефект, который чинил ревью PR #154);
- INSERT без `origin` после миграции обязан падать громко: `server_default`
  снят сразу после бэкфилла и второй щит обязательности — сама БД (§4).

Файл лежит в `tests/`, а не рядом с модулем: проверяется шаг схемы, общий для
всей БД, а не публичный контракт модуля (тот покрыт в `modules/requests/tests`).

**Сырой сев мимо RLS здесь — исключение, а не образец.** Строки надо положить в
схему `0024`, которой сегодняшняя ORM уже не выражает, поэтому тест ходит в
тенантные таблицы напрямую ролью-владельцем (`database_dsn`, см. её докстринг).
Модульному тесту, которому нужно «просто подсмотреть строку», так делать нельзя:
канон — `session_scope()` внутри `tenant_context`, образец —
`read_closed_by` в `modules/requests/tests/conftest.py`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from tests.conftest import database_dsn, temporary_database

# Плейсхолдер заморожен здесь так же, как в самой миграции, и по той же причине:
# он описывает строки, обезличенные ДО неё, а такие строки уже не изменятся.
# Живую `REQUEST_TEXT_ANONYMIZED_PLACEHOLDER` тест поэтому не подставляет и не
# сверяет с этим литералом: смени её значение — обезличенные по-новому строки
# будут закрыты уже ПОСЛЕ 0025 и придут со своим `closed_at`, а миграция и этот
# литерал остаются верны для старой эры. Обоих трогать не надо никогда.
_FROZEN_ANONYMIZED_PLACEHOLDER = "[обезличено: срок хранения истёк]"

# Строки, какими их видит миграция: четыре заявки одного отеля на момент выкатки.
_OPEN_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")
_DONE_ID = uuid.UUID("00000000-0000-4000-8000-000000000002")
_CANCELLED_ID = uuid.UUID("00000000-0000-4000-8000-000000000003")
_ANONYMIZED_ID = uuid.UUID("00000000-0000-4000-8000-000000000004")

_CREATED_AT = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
_CLOSED_UPDATED_AT = datetime(2026, 5, 1, 10, 30, tzinfo=UTC)
# У обезличенной `updated_at` — день прогона джобы ретеншна, спустя 90+ дней
# после закрытия: принять его за момент закрытия и есть та самая ошибка.
_ANONYMIZED_UPDATED_AT = _CREATED_AT + timedelta(days=91)


async def _seed_pre_migration_rows(dsn: str) -> None:
    """Отель с тремя заявками в схеме 0024 (колонок этой миграции ещё нет)."""
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        tenant_id = uuid.uuid4()
        category_id = uuid.uuid4()
        await connection.execute(
            "INSERT INTO tenants (id, slug, name, created_at, updated_at)"
            " VALUES ($1, 'hotel-pre-0025', 'Hotel Pre 0025', $2, $2)",
            tenant_id,
            _CREATED_AT,
        )
        await connection.execute(
            "INSERT INTO request_categories (id, tenant_id, key, name, created_at, updated_at)"
            " VALUES ($1, $2, 'housekeeping', 'Housekeeping', $3, $3)",
            category_id,
            tenant_id,
            _CREATED_AT,
        )
        for request_id, status, summary, updated_at in (
            (_OPEN_ID, "new", "принести полотенца", _CREATED_AT),
            (_DONE_ID, "done", "убрать 305", _CLOSED_UPDATED_AT),
            # Отменённая — второй терминальный статус: без неё щит проверял бы
            # предикат бэкфилла наполовину (`status IN ('done')` проходил бы).
            (_CANCELLED_ID, "cancelled", "гость передумал", _CLOSED_UPDATED_AT),
            (
                _ANONYMIZED_ID,
                "cancelled",
                _FROZEN_ANONYMIZED_PLACEHOLDER,
                _ANONYMIZED_UPDATED_AT,
            ),
        ):
            await connection.execute(
                "INSERT INTO service_requests"
                " (id, tenant_id, category_id, status, summary, created_at, updated_at)"
                " VALUES ($1, $2, $3, $4, $5, $6, $7)",
                request_id,
                tenant_id,
                category_id,
                status,
                summary,
                _CREATED_AT,
                updated_at,
            )
    finally:
        await connection.close()


async def _fetch_requests(dsn: str) -> dict[uuid.UUID, asyncpg.Record]:
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        rows = await connection.fetch(
            "SELECT id, origin, closed_at, claimed_at, updated_at FROM service_requests"
        )
    finally:
        await connection.close()
    return {row["id"]: row for row in rows}


async def _insert_without_origin(dsn: str) -> None:
    """Повторить вставку одной из строк, но уже без `origin` (см. тест ниже)."""
    connection = await asyncpg.connect(dsn, timeout=5)
    try:
        row = await connection.fetchrow(
            "SELECT tenant_id, category_id FROM service_requests LIMIT 1"
        )
        assert row is not None  # строки насеяны фикстурой выше
        await connection.execute(
            "INSERT INTO service_requests"
            " (id, tenant_id, category_id, status, summary, created_at, updated_at)"
            " VALUES ($1, $2, $3, 'new', 'без источника', $4, $4)",
            uuid.uuid4(),
            row["tenant_id"],
            row["category_id"],
            _CREATED_AT,
        )
    finally:
        await connection.close()


@dataclass(frozen=True)
class UpgradedDatabase:
    """Состояние БД сразу после шага 0024 → 0025 — предмет каждого теста ниже."""

    rows: dict[uuid.UUID, asyncpg.Record]
    dsn: str
    alembic_config: Config


@pytest.fixture
def upgraded() -> Iterator[UpgradedDatabase]:
    """Поднять БД до 0024, наполнить доисторическими строками, шагнуть на 0025."""
    with temporary_database("0024") as (database_name, alembic_config):
        dsn = database_dsn(database_name)
        asyncio.run(_seed_pre_migration_rows(dsn))
        command.upgrade(alembic_config, "0025")
        yield UpgradedDatabase(asyncio.run(_fetch_requests(dsn)), dsn, alembic_config)


def test_existing_rows_get_guest_chat_origin(upgraded: UpgradedDatabase) -> None:
    """§4: до этой спеки заявку создавал только AI-инструмент — бэкфилл не
    приближение, а факт. Пустого `origin` не остаётся ни у одной строки."""
    rows = upgraded.rows
    assert {row["origin"] for row in rows.values()} == {"guest_chat"}


@pytest.mark.parametrize("request_id", [_DONE_ID, _CANCELLED_ID])
def test_terminal_row_gets_closed_at_from_updated_at(
    upgraded: UpgradedDatabase, request_id: uuid.UUID
) -> None:
    """§3: у закрытой заявки момент закрытия — последняя запись в строку.

    Оба терминальных статуса, а не один: предикат бэкфилла перечисляет `done` и
    `cancelled`, и щит обязан краснеть на потере любого из них."""
    assert upgraded.rows[request_id]["closed_at"] == _CLOSED_UPDATED_AT


def test_anonymized_row_keeps_closed_at_empty(upgraded: UpgradedDatabase) -> None:
    """§3: у обезличенной `updated_at` — момент прогона ретеншна, а не закрытия.

    Честный NULL здесь лучше правдоподобного числа: подставить его — значит
    объявить пачку древних заявок «закрытыми в день прогона джобы» (PR #154)."""
    rows = upgraded.rows
    assert rows[_ANONYMIZED_ID]["closed_at"] is None


def test_open_row_gets_no_timestamps(upgraded: UpgradedDatabase) -> None:
    """Незакрытая заявка меток не получает; `claimed_at` не бэкфиллится ничем —
    его неоткуда взять, а приблизительное значение попало бы прямо в метрику."""
    rows = upgraded.rows
    assert rows[_OPEN_ID]["closed_at"] is None
    assert all(row["claimed_at"] is None for row in rows.values())


def test_insert_without_origin_fails_after_migration(upgraded: UpgradedDatabase) -> None:
    """§4, второй щит обязательности: `server_default` снят сразу после
    бэкфилла, поэтому путь, забывший назвать источник, падает громко, а не
    подмешивается молча в одну из двух измеряемых долей."""
    dsn = upgraded.dsn
    with pytest.raises(asyncpg.NotNullViolationError):
        asyncio.run(_insert_without_origin(dsn))


def test_downgrade_removes_the_columns(upgraded: UpgradedDatabase) -> None:
    """Шаг назад снимает все пять колонок: миграция обратима, а не односторонняя
    (без этого откат неудачного релиза упирался бы в схему)."""
    dsn, alembic_config = upgraded.dsn, upgraded.alembic_config
    command.downgrade(alembic_config, "0024")

    async def _columns() -> set[str]:
        connection = await asyncpg.connect(dsn, timeout=5)
        try:
            rows = await connection.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'service_requests'"
            )
        finally:
            await connection.close()
        return {row["column_name"] for row in rows}

    assert asyncio.run(_columns()).isdisjoint(
        {"origin", "closed_at", "claimed_at", "closed_by_user_id", "closed_by_display_name"}
    )
