"""Alembic runtime (Task 0008): async-движок, DSN из настроек приложения.

Метаданные берутся из канонического `Base`; каждый модуль с моделями обязан
быть импортирован здесь, иначе autogenerate его таблиц не увидит.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from hospitality.modules.requests import models as requests_models  # noqa: F401  (Task 0012)
from hospitality.platform import models  # noqa: F401  (регистрирует таблицы в Base.metadata)
from hospitality.shared import events  # noqa: F401  (outbox_events — Task 0010)
from hospitality.shared.config import get_settings
from hospitality.shared.db import Base

target_metadata = Base.metadata


def _database_url() -> str:
    # Тесты подставляют URL временной БД через config; иначе — настройки окружения.
    configured = context.config.get_main_option("sqlalchemy.url")
    return configured if configured else get_settings().postgres_dsn_async


def run_migrations_offline() -> None:
    """Генерация SQL без подключения к БД (`alembic upgrade head --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
