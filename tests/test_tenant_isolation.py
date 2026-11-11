"""ОБЯЗАТЕЛЬНЫЙ тест изоляции тенантов (Task 0009, P-4, ADR-003).

CI запускает этот файл отдельным блокирующим шагом: его падение — или
исчезновение (0 собранных тестов) — означает красный CI. Навсегда.

Проверяется на живом Postgres через канонический слой (`session_scope`),
то есть ровно тот путь, которым ходит весь код платформы:

- тенант A не видит данных B и наоборот (двунаправленно), в том числе
  нарочно «плохим» сырым SQL без фильтра по tenant_id — изоляцию держит RLS,
  а не дисциплина запросов;
- записать строку с чужим tenant_id нельзя (WITH CHECK политики);
- UPDATE/DELETE без WHERE не дотягиваются до чужих строк;
- запрос тенантных данных без контекста тенанта — исключение ещё до БД;
- платформенная сессия (без контекста) не читает и не пишет тенантные таблицы;
- `SET LOCAL` не утекает между запросами через пул соединений;
- соединение приложения работает ролью без SUPERUSER/BYPASSRLS — иначе RLS
  молча не действует (суперпользователь и владелец таблиц политики игнорируют);
- каждая таблица с колонкой `tenant_id` — под ENABLE и FORCE RLS с канонической
  политикой, по каталогу Postgres, а не по списку в тесте (#1052): новая
  таблица без RLS краснеет здесь, даже если её тесты о ней не знают.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from hospitality.platform.models import Tenant, TenantIsolationCanary
from hospitality.shared.db import platform_session_scope, session_scope
from hospitality.shared.tenancy import TenantContextRequiredError, tenant_context


# Таблицы с `tenant_id` без RLS — платформенный мир, который сам ставит контекст
# тенанта (миграция 0017, ADR-008 §6): членство проверяется до контекста, инвайт
# принимает человек без сессии. Список явный, а не выведенный: таблица попадает
# сюда только правкой этой строки, на глазах у ревью.
TENANT_ID_TABLES_WITHOUT_RLS = frozenset({"staff_invites", "tenant_memberships"})
# Политики сверх канонической — каждая РАСШИРЯЕТ доступ (PERMISSIVE-политики
# складываются через OR), поэтому тоже явным списком. `platform_dispatch` —
# воркер outbox доставляет события всех отелей (миграция 0003).
EXTRA_POLICIES = frozenset({("outbox_events", "platform_dispatch")})
# Выражение канона `_apply_tenant_rls` (миграция 0002) так, как его печатает Postgres.
CANONICAL_POLICY_EXPRESSION = (
    "(tenant_id = (NULLIF(current_setting('app.tenant_id'::text, true), ''::text))::uuid)"
)

_TENANT_TABLES_SQL = """
SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema()
  AND c.relkind IN ('r', 'p')
  AND EXISTS (
      SELECT 1 FROM pg_attribute a
      WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
  )
"""
_POLICIES_SQL = """
SELECT c.relname, p.polname, p.polcmd::text, p.polpermissive, p.polroles = '{0}',
       pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid)
FROM pg_policy p
JOIN pg_class c ON c.oid = p.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = current_schema()
"""


@pytest.fixture
async def two_tenants(canonical_database: None) -> tuple[uuid.UUID, uuid.UUID]:
    """Два тенанта в реестре — «Hotel A» и «Hotel B»."""
    async with platform_session_scope() as session:
        tenant_a = Tenant(slug="hotel-a", name="Hotel A")
        tenant_b = Tenant(slug="hotel-b", name="Hotel B")
        session.add_all([tenant_a, tenant_b])
        await session.flush()
        return (tenant_a.id, tenant_b.id)


async def _add_canary(note: str) -> None:
    """Строка тенантных данных от имени ТЕКУЩЕГО тенанта (tenant_id — из контекста)."""
    async with session_scope() as session:
        session.add(TenantIsolationCanary(note=note))


async def _visible_notes() -> set[str]:
    async with session_scope() as session:
        rows = (await session.execute(select(TenantIsolationCanary.note))).scalars().all()
    return set(rows)


async def test_tenant_sees_only_own_rows_bidirectional(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        await _add_canary("a-note")
    with tenant_context(tenant_b):
        await _add_canary("b-note")

    with tenant_context(tenant_a):
        assert await _visible_notes() == {"a-note"}
    with tenant_context(tenant_b):
        assert await _visible_notes() == {"b-note"}


async def test_raw_sql_without_tenant_filter_is_still_isolated(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """DoD задачи: даже нарочно «плохой» запрос не читает чужого — фильтрует RLS."""
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        await _add_canary("a-note")
    with tenant_context(tenant_b):
        await _add_canary("b-note")

    with tenant_context(tenant_a):
        async with session_scope() as session:
            notes = (
                (await session.execute(text("SELECT note FROM tenant_isolation_canary")))
                .scalars()
                .all()
            )
    assert notes == ["a-note"]


async def test_insert_with_foreign_tenant_id_is_rejected(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a), pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope() as session:
            session.add(TenantIsolationCanary(tenant_id=tenant_b, note="stolen"))
            await session.flush()

    with tenant_context(tenant_b):
        assert await _visible_notes() == set()


async def test_update_and_delete_do_not_reach_foreign_rows(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        await _add_canary("a-note")
    with tenant_context(tenant_b):
        await _add_canary("b-note")

    with tenant_context(tenant_b):
        async with session_scope() as session:
            updated = (
                (
                    await session.execute(
                        text("UPDATE tenant_isolation_canary SET note = 'hacked' RETURNING note")
                    )
                )
                .scalars()
                .all()
            )
            deleted = (
                (await session.execute(text("DELETE FROM tenant_isolation_canary RETURNING note")))
                .scalars()
                .all()
            )
    assert updated == ["hacked"]  # только собственная строка B
    assert deleted == ["hacked"]

    with tenant_context(tenant_a):
        assert await _visible_notes() == {"a-note"}  # данные A не тронуты


async def test_app_connection_runs_without_rls_bypass(canonical_database: None) -> None:
    """Ловушка, найденная этим тестом при внедрении: docker-образ Postgres делает
    POSTGRES_USER суперпользователем, а для суперпользователя и владельца таблиц
    RLS молча не действует. Канонический engine обязан понижать каждое соединение
    до обычной роли (SET ROLE, см. get_engine) — здесь это проверяется навсегда."""
    async with platform_session_scope() as session:
        row = (
            await session.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    assert tuple(row) == ("hospitality_app", False, False)


async def test_every_tenant_table_is_under_forced_rls_with_canonical_policy(
    canonical_database: None,
) -> None:
    """P-4 по каталогу базы (#1052, sec M7). Проба мутаций аудита 11.11 сняла RLS
    с одной из шести таблиц записок, и набор прошёл зелёным: тест записок
    перечислял таблицы руками, четыре из шести. Здесь таблицы берутся из базы на
    `head`: ENABLE (политики вообще действуют), FORCE (и для владельца таблиц),
    политика `tenant_isolation` на все команды с выражением канона в USING и
    WITH CHECK. Сообщение падения называет таблицу и чего у неё нет."""
    async with platform_session_scope() as session:
        tables = (await session.execute(text(_TENANT_TABLES_SQL))).all()
        policies = (await session.execute(text(_POLICIES_SQL))).all()

    flags = {table: (enabled, forced) for table, enabled, forced in tables}
    # Каталог прочитан по-настоящему, а не пустым ответом: канон и исключения на месте.
    assert {"tenant_isolation_canary", *TENANT_ID_TABLES_WITHOUT_RLS} <= flags.keys()
    canonical = {
        table
        for table, name, command, permissive, to_public, using, check in policies
        if name == "tenant_isolation"
        and (command, permissive, to_public) == ("*", True, True)
        and using == check == CANONICAL_POLICY_EXPRESSION
    }
    problems: list[str] = []
    for table, (enabled, forced) in sorted(flags.items()):
        if table in TENANT_ID_TABLES_WITHOUT_RLS:
            if enabled:
                problems.append(f"{table}: под RLS, а стоит в исключениях — убрать из списка")
            continue
        if not enabled:
            problems.append(f"{table}: нет ENABLE ROW LEVEL SECURITY")
        if not forced:
            problems.append(f"{table}: нет FORCE ROW LEVEL SECURITY")
        if table not in canonical:
            problems.append(f"{table}: нет политики tenant_isolation канона миграции 0002")
    assert problems == []

    extra = {(table, name) for table, name, *_ in policies if name != "tenant_isolation"}
    assert extra == EXTRA_POLICIES


async def test_session_scope_without_tenant_context_raises(canonical_database: None) -> None:
    with pytest.raises(TenantContextRequiredError):
        async with session_scope():
            pytest.fail("транзакция не должна была открыться")


async def test_platform_scope_cannot_read_tenant_table(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        await _add_canary("a-note")
    with tenant_context(tenant_b):
        await _add_canary("b-note")

    async with platform_session_scope() as session:
        count = await session.scalar(select(func.count()).select_from(TenantIsolationCanary))
    assert count == 0


async def test_platform_scope_cannot_write_tenant_table(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with pytest.raises(DBAPIError, match="row-level security"):
        async with platform_session_scope() as session:
            # tenant_id указан явно и корректно — но контекста нет, политика запрещает.
            session.add(TenantIsolationCanary(tenant_id=tenant_a, note="no-context"))
            await session.flush()


async def test_tenant_setting_does_not_leak_through_connection_pool(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Известный риск модели (ADR-003): пул переиспользует соединения между
    запросами разных тенантов. `SET LOCAL` живёт до конца транзакции — на том же
    физическом соединении следующий scope стартует без контекста."""
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        await _add_canary("a-note")
        async with session_scope() as session:
            pid_tenant_a = await session.scalar(text("SELECT pg_backend_pid()"))

    async with platform_session_scope() as session:
        pid_platform = await session.scalar(text("SELECT pg_backend_pid()"))
        leaked = await session.scalar(text("SELECT current_setting('app.tenant_id', true)"))

    # Последовательные scope обязаны получить то же соединение из пула — иначе
    # проверка утечки ничего не проверяет. Если пул изменился, тест должен упасть
    # и быть переписан осознанно, а не позеленеть вхолостую.
    assert pid_platform == pid_tenant_a
    # После конца транзакции с SET LOCAL Postgres оставляет '' (поэтому в политике
    # NULLIF): контекст тенанта не пережил границу транзакции.
    assert leaked in (None, "")

    with tenant_context(tenant_b):
        assert await _visible_notes() == set()  # данных A через тот же коннект не видно
