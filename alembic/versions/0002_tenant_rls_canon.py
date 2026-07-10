"""tenant RLS canon: tenant_isolation_canary

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-09

Канон RLS для тенантных таблиц (Task 0009, P-4, ADR-003). Каждая новая
тенантная таблица в своей миграции копирует блок ENABLE + FORCE + политика
(см. `_apply_tenant_rls` ниже — берите функцию к себе в миграцию целиком).

Почему именно так:
- рантайм-роль `hospitality_app` (создаётся здесь же): docker-образ Postgres
  делает `POSTGRES_USER` СУПЕРПОЛЬЗОВАТЕЛЕМ, а суперпользователь и владелец
  таблиц игнорируют RLS — политики были бы декорацией. Поэтому приложение
  подключается логин-ролью окружения, но первым делом выполняет
  `SET ROLE hospitality_app` (см. `shared/db.py: get_engine`) — обычная роль
  без SUPERUSER/BYPASSRLS, для которой политики обязательны. Роль NOLOGIN —
  пароля и секретов у неё нет. Миграции этот SET ROLE не делают и идут под
  владельцем схемы (DDL);
- ENABLE ROW LEVEL SECURITY — включает политики на таблице;
- FORCE ROW LEVEL SECURITY — защита в глубину: политика действует и на
  владельца таблиц, если кто-то пойдёт в БД мимо канонического engine;
- политика читает тенанта из GUC `app.tenant_id`, который `session_scope()`
  ставит через `set_config(..., is_local => true)` (= SET LOCAL, живёт до
  конца транзакции — не утекает через пул соединений);
- NULLIF(..., '') обязателен: после конца транзакции с SET LOCAL Postgres
  оставляет у GUC пустую строку как сессионное значение; без NULLIF выражение
  `''::uuid` роняло бы любой следующий запрос на этом соединении, а с ним
  «нет контекста» детерминированно означает «0 строк видно, запись запрещена».
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _apply_tenant_rls(table_name: str) -> None:
    """КАНОН (копируется в миграцию каждой новой тенантной таблицы)."""
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table_name}
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def _create_app_role() -> None:
    """Рантайм-роль приложения (см. докстринг миграции). Роль кластерная,
    а миграции гоняются на много БД (dev, CI, временные БД тестов) — поэтому
    создание идемпотентно, а downgrade роль не трогает."""
    op.execute(
        """
        DO $$
        BEGIN
            CREATE ROLE hospitality_app NOLOGIN;
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO hospitality_app")
    # Будущие таблицы, созданные миграциями (той же ролью, что выполняет эту),
    # получают DML-права автоматически; DDL и alembic_version остаются недоступны.
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hospitality_app"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tenants TO hospitality_app")


def upgrade() -> None:
    _create_app_role()
    op.create_table(
        "tenant_isolation_canary",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f("fk_tenant_isolation_canary_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant_isolation_canary")),
    )
    op.create_index(
        op.f("ix_tenant_isolation_canary_tenant_id"),
        "tenant_isolation_canary",
        ["tenant_id"],
    )
    _apply_tenant_rls("tenant_isolation_canary")


def downgrade() -> None:
    # Политики и индексы удаляются вместе с таблицей.
    op.drop_table("tenant_isolation_canary")
