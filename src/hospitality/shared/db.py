"""Канонический слой работы с БД (Task 0008, FOUNDATION §6, §9, ADR-003).

Единственный способ работать с базой — контекстный менеджер `session_scope()`:
транзакция открывается и закрывается только им. Ручное создание engine/сессии
вне этого модуля запрещено (P-12): в этой точке Task 0009 добавит `SET LOCAL`
контекста тенанта, и любой обходной путь станет дырой в изоляции.

Канон времени (§9): в БД — только UTC. Колонки времени объявляются типом
`UTCDateTime`, который отвергает наивные datetime на записи и всегда отдаёт
aware-UTC на чтении. Naive datetime в модели или запросе — блокер ревью.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import DateTime, MetaData
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from hospitality.shared.config import get_settings

# Явные имена constraints: без конвенции Alembic не сможет написать downgrade
# (у безымянного constraint нет имени для drop), а diff миграций нечитаем.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс всех ORM-моделей платформы."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """Канонический «сейчас» для БД и доменной логики: aware UTC."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """TIMESTAMPTZ, принудительно в UTC (FOUNDATION §9).

    Запись наивного datetime — ошибка программирования, падает сразу
    (а не молча пишет время в неизвестном поясе). Чтение всегда отдаёт
    aware datetime в UTC; локальное время отеля — забота слоя представления
    через часовой пояс из конфига тенанта.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "naive datetime запрещён (FOUNDATION §9): используйте utc_now() "
                "или datetime с явным tzinfo"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.astimezone(UTC) if value is not None else None


@lru_cache
def get_engine() -> AsyncEngine:
    """Engine приложения — синглтон, создаётся лениво из настроек окружения."""
    return create_async_engine(get_settings().postgres_dsn_async)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Единственный канонический способ получить сессию БД (§6, P-12).

    Одна транзакция на scope: commit при нормальном выходе, rollback при
    исключении. Внутри scope commit/rollback руками не вызываются.
    Task 0009 добавит здесь `SET LOCAL` контекста тенанта — поэтому второй
    способ открыть сессию в кодовой базе не должен появиться никогда.

    Канонический пример:

        async with session_scope() as session:
            session.add(tenant)
    """
    # expire_on_commit=False: объекты пригодны к чтению после выхода из scope,
    # без неожиданных ленивых обращений к закрытой сессии.
    async with (
        AsyncSession(get_engine(), expire_on_commit=False) as session,
        session.begin(),
    ):
        yield session
