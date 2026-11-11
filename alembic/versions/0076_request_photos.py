"""requests и channels: фото гостя к заявке — request_photos и messages.file_id

Revision ID: 0076
Revises: 0073
Create Date: 2026-11-11

Spec 0081 §5–§6 (issue #82, PR 1 серии): гость присылает фото в чат, код
прикладывает его к заявке, служба видит его в кабинете.

- `request_photos` — связь заявки с файлом узла (`uploaded_files`, назначение
  `request_photo`). Тенантная по канону 0002: RLS ENABLE + FORCE + политика
  `tenant_isolation`; права роли приложения — через ALTER DEFAULT PRIVILEGES
  миграции 0002. Оба FK — ON DELETE CASCADE (канон `act_request_files`, 0042):
  ретеншн узла удаляет файл — уходит и связь; удаление заявки уносит связи.
  UNIQUE (`request_id`, `file_id`) — повтор приложения не плодит строк (P-8).
- `messages.file_id` — входящее фото гостя в переписке. FK на `uploaded_files`
  ON DELETE SET NULL: файл ушёл ретеншном раньше строки сообщения — строка
  остаётся «было фото», без файла. Индекс — по нему ищут ждущие фото диалога.

Схема обратно-совместима: новая таблица, которую старый образ не читает, и
колонка NULLABLE без умолчания. Бэкфилла нет — фото до этой миграции не было.
Downgrade удаляет таблицу и колонку: связи фото с заявками теряются, файлы
узла остаются строками `uploaded_files` и уходят ретеншном по своему сроку;
сообщения `content_kind = 'photo'` старый образ показывает как не-текст.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0073"
branch_labels = None
depends_on = None

_PHOTOS = "request_photos"
_MESSAGES = "messages"


def _apply_tenant_rls(table_name: str) -> None:
    """КАНОН (копия из миграции 0002 — см. обоснование в её докстринге)."""
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table_name}
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )


def upgrade() -> None:
    op.create_table(
        _PHOTOS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.id"],
            name=op.f(f"fk_{_PHOTOS}_tenant_id_tenants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["service_requests.id"],
            name=op.f(f"fk_{_PHOTOS}_request_id_service_requests"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["uploaded_files.id"],
            name=op.f(f"fk_{_PHOTOS}_file_id_uploaded_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{_PHOTOS}")),
        sa.UniqueConstraint("request_id", "file_id", name="uq_request_photos_request_file"),
    )
    op.create_index(op.f(f"ix_{_PHOTOS}_tenant_id"), _PHOTOS, ["tenant_id"])
    op.create_index(op.f(f"ix_{_PHOTOS}_request_id"), _PHOTOS, ["request_id"])
    op.create_index(op.f(f"ix_{_PHOTOS}_file_id"), _PHOTOS, ["file_id"])
    _apply_tenant_rls(_PHOTOS)

    op.add_column(_MESSAGES, sa.Column("file_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{_MESSAGES}_file_id_uploaded_files"),
        _MESSAGES,
        "uploaded_files",
        ["file_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f(f"ix_{_MESSAGES}_file_id"), _MESSAGES, ["file_id"])


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_MESSAGES}_file_id"), table_name=_MESSAGES)
    op.drop_constraint(
        op.f(f"fk_{_MESSAGES}_file_id_uploaded_files"), _MESSAGES, type_="foreignkey"
    )
    op.drop_column(_MESSAGES, "file_id")
    # Политика и индексы удаляются вместе с таблицей.
    op.drop_table(_PHOTOS)
