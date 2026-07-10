"""Канонический слой работы с БД (Task 0008/0009, FOUNDATION §6, §9, ADR-003).

Два канонических способа получить сессию — и никаких других (P-12):

- `session_scope()` — работа с данными тенанта. Требует `tenant_context(...)`
  (иначе исключение) и первой командой транзакции ставит `SET LOCAL`
  контекст тенанта — RLS-политики Postgres видят его через
  `current_setting('app.tenant_id', true)`.
- `platform_session_scope()` — платформенные операции вне тенанта: реестр
  тенантов, сиды, диагностика. RLS при этом никуда не девается: тенантные
  таблицы отсюда не читаются (0 строк) и не пишутся (ошибка политики).

Ручное создание engine/сессии вне этого модуля запрещено: обходной путь
мимо `SET LOCAL` и `SET ROLE` (см. `get_engine`) — дыра в изоляции тенантов.

Канон времени (§9): в БД — только UTC. Колонки времени объявляются типом
`UTCDateTime`, который отвергает наивные datetime на записи и всегда отдаёт
aware-UTC на чтении. Naive datetime в модели или запросе — блокер ревью.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy import DateTime, MetaData, event, text
from sqlalchemy.engine import Dialect
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import ConnectionPoolEntry
from sqlalchemy.types import TypeDecorator

from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import TENANT_ID_GUC, current_tenant_id

# Рантайм-роль приложения: обычная роль без SUPERUSER/BYPASSRLS, для которой
# RLS-политики обязательны. Логин-роль окружения (POSTGRES_USER) — владелец
# схемы, и в docker-образе Postgres она суперпользователь: обе категории
# игнорируют RLS, поэтому работать под ней приложению запрещено. Роль создаёт
# миграция 0002; если её нет — миграции не применены, соединение упадёт сразу
# и внятно.
APP_DB_ROLE = "hospitality_app"

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
    """Engine приложения — синглтон, создаётся лениво из настроек окружения.

    Каждое новое соединение пула сразу понижается до `APP_DB_ROLE` (`SET ROLE`):
    RLS действует только на обычную роль — без этого суперпользователь
    docker-образа видел бы все тенанты насквозь. Проверяется обязательным
    тестом изоляции (`test_app_connection_runs_without_rls_bypass`).
    """
    engine = create_async_engine(get_settings().postgres_dsn_async)

    @event.listens_for(engine.sync_engine, "connect")
    def _assume_app_role(dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET ROLE {APP_DB_ROLE}")
        finally:
            cursor.close()

    return engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Канонический способ работать с данными тенанта (§6, P-4, P-12).

    Требует активный `tenant_context(...)` — вне его падает
    `TenantContextRequiredError` ещё до открытия транзакции. Одна транзакция
    на scope: commit при нормальном выходе, rollback при исключении; внутри
    scope commit/rollback руками не вызываются.

    Контекст тенанта ставится первой командой транзакции через
    `set_config(..., is_local => true)` — это параметризуемый эквивалент
    `SET LOCAL`: значение живёт ровно до конца транзакции и не может утечь
    в другой запрос через пул соединений (ADR-003; проверяется обязательным
    тестом изоляции).

    Канонический пример:

        with tenant_context(tenant_id):
            async with session_scope() as session:
                session.add(service_request)
    """
    tenant_id = current_tenant_id()
    # expire_on_commit=False: объекты пригодны к чтению после выхода из scope,
    # без неожиданных ленивых обращений к закрытой сессии.
    async with (
        AsyncSession(get_engine(), expire_on_commit=False) as session,
        session.begin(),
    ):
        await session.execute(
            text("SELECT set_config(:guc, :tenant_id, true)"),
            {"guc": TENANT_ID_GUC, "tenant_id": str(tenant_id)},
        )
        yield session


@asynccontextmanager
async def platform_session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия для платформенных операций ВНЕ контекста тенанта (§6, ADR-003).

    Только для работы с нетенантными таблицами: реестр тенантов (онбординг,
    сиды — Task 0011), служебная диагностика. Контекст тенанта не ставится,
    поэтому RLS-политики не пропустят ни чтение, ни запись тенантных таблиц —
    «обойти изоляцию» через этот scope нельзя.

    Для данных тенанта используйте `session_scope()` внутри `tenant_context`.
    """
    async with (
        AsyncSession(get_engine(), expire_on_commit=False) as session,
        session.begin(),
    ):
        yield session
