"""Общие фикстуры тестов (Task 0007/0008/0009).

Приложение собирается настоящим composition root (`create_app`) — так тесты
проверяют и сам канон, и его подключение. Служебные роуты добавляются поверх,
чтобы не тащить тестовый код в приложение.

Фикстуры БД: каждый тест получает СВЕЖУЮ временную базу — `migrated_database_name`
создаёт её копией шаблона, а шаблон один раз за процесс pytest поднимается
`alembic upgrade head` (#526); `canonical_database` направляет канонический путь
(настройки → engine → session_scope) на неё. Нужен работающий Postgres
(`make dev`); без него DB-тесты пропускаются локально и падают в CI.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path

import asyncpg
import pytest
import structlog
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hospitality.app import create_app
from hospitality.integrations.file_storage import FakeFileStorageAdapter
from hospitality.shared import events
from hospitality.shared.config import get_settings
from hospitality.shared.db import APP_DB_ROLE, get_engine
from hospitality.shared.errors import AppError
from hospitality.shared.middleware import get_correlation_id
from hospitality.shared.ports.file_storage import file_storage_or_none, set_file_storage

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeRateLimitRedis:
    """In-memory реализация протокола `RateLimitRedis` (shared/ratelimit.py).

    CI гоняет юнит-тесты без живого Redis (в сервисах ci.yml только Postgres) —
    счётчик проверяется на этом фейке; живой Redis его видит только smoke-набор.
    `fail` переводит фейк в режим «Redis упал» — для тестов fail-open.
    """

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    async def get(self, name: str) -> bytes | None:
        # Живой redis-py отдаёт байты — `peek_rate_limit` разбирает их int(),
        # и фейк обязан быть таким же, иначе тест зеленее продакшена.
        if self.fail:
            raise OSError("fake redis is down")
        value = self.counters.get(name)
        return None if value is None else str(value).encode()

    async def incr(self, name: str, amount: int = 1) -> int:
        if self.fail:
            raise OSError("fake redis is down")
        self.counters[name] = self.counters.get(name, 0) + amount
        return self.counters[name]

    async def expire(self, name: str, time: int) -> bool:
        if self.fail:
            raise OSError("fake redis is down")
        self.ttls[name] = time
        return True

    async def aclose(self) -> None:
        # Вызывается, когда фейк подменяет create_redis_client (клиент на вызов).
        return None


@pytest.fixture(autouse=True)
def _clean_log_context() -> Iterator[None]:
    # Контекст логирования не должен утекать между тестами.
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture(autouse=True)
def _isolated_event_subscribers() -> Iterator[None]:
    """Реестр подписчиков событий — глобальный на процесс (Task 0010): composition
    root воркера заполняет его один раз. Тесты не должны протекать подписками
    друг в друга: снимок до теста — восстановление после."""
    saved_subscribers = {name: list(handlers) for name, handlers in events._subscribers.items()}
    saved_event_types = dict(events._event_types)
    yield
    events._subscribers.clear()
    events._subscribers.update(saved_subscribers)
    events._event_types.clear()
    events._event_types.update(saved_event_types)


# ---------------------------------------------------------------------------
# Временная БД (Task 0008/0009)
# ---------------------------------------------------------------------------


async def _admin_execute(sql: str) -> None:
    settings = get_settings()
    connection = await asyncpg.connect(settings.postgres_dsn, timeout=2)
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _postgres_available() -> bool:
    try:
        asyncio.run(_admin_execute("SELECT 1"))
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return False
    return True


def database_dsn(database_name: str) -> str:
    """DSN временной БД для asyncpg (без драйвера SQLAlchemy в схеме).

    **Только для тестов самой схемы.** Роль здесь — логин-роль окружения, то
    есть владелец схемы, а в докерном образе Postgres ещё и СУПЕРПОЛЬЗОВАТЕЛЬ:
    RLS она обходит, `SET ROLE hospitality_app` не делает (миграция 0002). Ходить
    по этому DSN в тенантные таблицы значит смотреть на них мимо изоляции — за
    пределами тестов миграций так нельзя, и `shared/db.py` это прямо запрещает.
    Канон доступа к тенантным данным из теста — `session_scope()` внутри
    `tenant_context`; образец рядом: `modules/requests/tests/conftest.py`.
    """
    settings = get_settings()
    return (
        f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{database_name}"
    )


def alembic_config_for(database_name: str) -> Config:
    """Конфиг alembic, нацеленный на конкретную БД (канон фикстур ниже)."""
    settings = get_settings()
    alembic_config = Config(str(REPO_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    alembic_config.set_main_option(
        "sqlalchemy.url",
        f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
        f"@{settings.postgres_host}:{settings.postgres_port}/{database_name}",
    )
    return alembic_config


@functools.cache
def _ensure_app_role() -> None:
    """Роль приложения кластерная — одна на все БД Postgres (миграция 0002, те же атрибуты).

    Воркеры xdist (#527) поднимают свои шаблоны одновременно, а alembic держит
    всю историю одной транзакцией: на свежем кластере (CI) второй `CREATE ROLE`
    ждёт первого до конца его миграций и падает с `unique_violation` — миграция
    ловит только `duplicate_object`. Здесь роль создаётся заранее короткой
    транзакцией, и миграция уходит в ветку «роль уже есть».
    """
    asyncio.run(
        _admin_execute(
            f"DO $$ BEGIN CREATE ROLE {APP_DB_ROLE} NOLOGIN; "
            "EXCEPTION WHEN duplicate_object OR unique_violation THEN NULL; END $$"
        )
    )


def _require_postgres() -> None:
    if not _postgres_available():
        if os.environ.get("CI"):
            pytest.fail("В CI Postgres обязан быть доступен (сервис в ci.yml)")
        pytest.skip("Postgres недоступен — поднимите локальную среду: make dev")
    _ensure_app_role()


# Воркер xdist в имени БД: брошенную базу упавшего прогона видно, чей она.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")


@contextmanager
def _new_database(template: str | None = None) -> Iterator[str]:
    """Пустая БД или копия шаблона; удаляется на выходе."""
    database_name = f"hospitality_test_{_WORKER}_{uuid.uuid4().hex[:12]}"
    template_clause = f' TEMPLATE "{template}"' if template else ""
    asyncio.run(_admin_execute(f'CREATE DATABASE "{database_name}"{template_clause}'))
    try:
        yield database_name
    finally:
        asyncio.run(_admin_execute(f'DROP DATABASE "{database_name}" WITH (FORCE)'))


_HEAD_TEMPLATE = pytest.StashKey[str]()


def _drop_template(template_name: str) -> None:
    # Postgres к концу прогона мог уже остановиться — тогда шаблона и так нет.
    with contextlib.suppress(OSError, asyncpg.PostgresError, TimeoutError):
        asyncio.run(_admin_execute(f'DROP DATABASE IF EXISTS "{template_name}" WITH (FORCE)'))


def _head_template(config: pytest.Config) -> str:
    """Шаблон на `head`: миграции — один раз за прогон pytest, дальше копии (#526).

    Postgres копирует базу, только пока к ней никто не подключён: alembic своё
    соединение закрывает (NullPool в `alembic/env.py`), а `ALLOW_CONNECTIONS false`
    не пускает новые, в том числе autovacuum. Сломанная миграция падает здесь
    и не запоминается — каждый DB-тест красный, как без шаблона. Удаляет шаблон
    сам pytest в конце прогона (`atexit` поздно: asyncio уже не открывает соединений).
    """
    if _HEAD_TEMPLATE in config.stash:
        return config.stash[_HEAD_TEMPLATE]
    template_name = f"hospitality_test_template_{_WORKER}_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(f'CREATE DATABASE "{template_name}"'))
    config.add_cleanup(functools.partial(_drop_template, template_name))
    command.upgrade(alembic_config_for(template_name), "head")
    asyncio.run(_admin_execute(f'ALTER DATABASE "{template_name}" WITH ALLOW_CONNECTIONS false'))
    config.stash[_HEAD_TEMPLATE] = template_name
    return template_name


@contextmanager
def temporary_database(revision: str) -> Iterator[tuple[str, Config]]:
    """Чистая временная БД, поднятая до указанной ревизии alembic.

    Нужна тестам самих миграций: они наполняют «доисторическую» БД строками
    и смотрят, что с ними сделал следующий шаг (`tests/test_migration_*`).
    Обычному тесту — `migrated_database_name` ниже: копия шаблона на `head`.
    """
    _require_postgres()
    with _new_database() as database_name:
        alembic_config = alembic_config_for(database_name)
        command.upgrade(alembic_config, revision)
        yield database_name, alembic_config


@pytest.fixture
def migrated_database_name(request: pytest.FixtureRequest) -> Iterator[str]:
    """Чистая временная БД на `head` — копия шаблона за доли секунды вместо
    прогона всех миграций (#526). Применимость миграций на чистый Postgres
    проверяют сам шаблон и job `migrations` в CI."""
    _require_postgres()
    with _new_database(template=_head_template(request.config)) as database_name:
        yield database_name


@pytest.fixture
async def canonical_database(
    migrated_database_name: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Направляет канонический путь (настройки → engine → session_scope) на временную БД."""
    monkeypatch.setenv("POSTGRES_DB", migrated_database_name)
    get_settings.cache_clear()
    get_engine.cache_clear()
    try:
        yield
    finally:
        await get_engine().dispose()
        get_settings.cache_clear()
        get_engine.cache_clear()


@pytest.fixture
def app_with_test_routes() -> FastAPI:
    app = create_app()

    @app.get("/echo-correlation-id")
    async def echo_correlation_id(request: Request) -> dict[str, str | None]:
        return {"correlation_id": get_correlation_id(request)}

    @app.get("/raise-app-error")
    async def raise_app_error() -> None:
        raise AppError(code="ERR-TEST-001", message="expected test error", status_code=418)

    @app.get("/raise-unhandled")
    async def raise_unhandled() -> None:
        raise RuntimeError("secret internals: database password is hunter2")

    @app.get("/validate/{item_id}")
    async def validate_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    return app


@pytest.fixture
def client(app_with_test_routes: FastAPI) -> TestClient:
    # raise_server_exceptions=False: необработанное исключение должно вернуться
    # клиенту как канонический 500-ответ, а не упасть внутрь теста.
    return TestClient(app_with_test_routes, raise_server_exceptions=False)


# Ключ шифрования файлов только для тестов: 32 нулевых байта в base64.
TEST_FILE_ENCRYPTION_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@pytest.fixture
def fake_file_storage() -> Iterator[FakeFileStorageAdapter]:
    """Fake-адаптер хранилища файлов в порту и тестовый ключ шифрования (spec 0047).

    Прежний адаптер процесса возвращается после теста: `create_app()` других
    тестов кладёт свой, и состояние не должно перетекать между тестами.
    Окружение — своим `MonkeyPatch.context()`, а не общим `monkeypatch`: иначе
    порядок отката зависел бы от порядка фикстур, и общий откат вернул бы
    `POSTGRES_DB` уже после удаления временной БД `canonical_database`.
    """
    previous = file_storage_or_none()
    storage = FakeFileStorageAdapter()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("FILE_ENCRYPTION_KEY", TEST_FILE_ENCRYPTION_KEY)
        get_settings.cache_clear()
        set_file_storage(storage)
        try:
            yield storage
        finally:
            if previous is not None:
                set_file_storage(previous)
            get_settings.cache_clear()
    get_settings.cache_clear()
