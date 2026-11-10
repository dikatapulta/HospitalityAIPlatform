"""memos: служебные записки и объяснительные — данные

Revision ID: 0071
Revises: 0070
Create Date: 2026-11-10

Spec 0077 §8 (issue #936), PR 3 серии. Шесть тенантных таблиц модуля `memos`,
RLS по канону 0002:

- `memo_demands` — требование письменного объяснения (фото, получение, срок; код — PR 5);
- `memos` — записка или объяснительная: номер (UNIQUE в отеле), автор, адресат,
  шифртексты заголовка, текста, хэша содержимого и комментария решения, статус, времена;
- `memo_signature_samples` — образец подписи (шифртекст штрихов); действующий у
  сотрудника один — частичный уникальный индекс;
- `memo_signatures` — лист подписей: роль, ответ, шифртексты замечания и доказательства;
- `memo_views` — журнал просмотров;
- `memo_counters` — последний номер на `(tenant_id, kind, year)`.

FK на `users` без каскада: учётки гасятся, а не удаляются (ADR-008 §1), и запись
переживает увольнение автора. Обратно-совместим: старый образ таблиц не видит.
Downgrade стирает записки, подписи и образцы целиком — до включения раздела у
отеля (флаг `memos_enabled`, PR 4) их нет.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

_TABLES = (
    "memo_demands",
    "memos",
    "memo_signature_samples",
    "memo_signatures",
    "memo_views",
    "memo_counters",
)


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


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"],
        ["tenants.id"],
        name=op.f(f"fk_{table}_tenant_id_tenants"),
        ondelete="CASCADE",
    )


def _user_fk(table: str, column: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint([column], ["users.id"], name=op.f(f"fk_{table}_{column}_users"))


def _id_and_tenant() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "memo_demands",
        *_id_and_tenant(),
        sa.Column("demanded_by_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_proof_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _tenant_fk("memo_demands"),
        _user_fk("memo_demands", "demanded_by_id"),
        _user_fk("memo_demands", "employee_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memo_demands")),
    )
    op.create_index(op.f("ix_memo_demands_employee_id"), "memo_demands", ["employee_id"])

    op.create_table(
        "memos",
        *_id_and_tenant(),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("number", sa.String(length=24), nullable=True),
        sa.Column("template_key", sa.String(length=40), nullable=True),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("addressee_id", sa.Uuid(), nullable=False),
        sa.Column("title_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("body_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("content_hash_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("based_on_id", sa.Uuid(), nullable=True),
        sa.Column("demand_id", sa.Uuid(), nullable=True),
        sa.Column("decision_comment_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        _tenant_fk("memos"),
        _user_fk("memos", "author_id"),
        _user_fk("memos", "addressee_id"),
        sa.ForeignKeyConstraint(
            ["based_on_id"],
            ["memos.id"],
            name=op.f("fk_memos_based_on_id_memos"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["demand_id"],
            ["memo_demands.id"],
            name=op.f("fk_memos_demand_id_memo_demands"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memos")),
        sa.UniqueConstraint("tenant_id", "number", name=op.f("uq_memos_tenant_id_number")),
    )
    op.create_index(op.f("ix_memos_author_id"), "memos", ["author_id"])
    op.create_index(op.f("ix_memos_addressee_id"), "memos", ["addressee_id"])
    op.create_index(op.f("ix_memos_sent_at"), "memos", ["sent_at"])

    op.create_table(
        "memo_signature_samples",
        *_id_and_tenant(),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("strokes_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _tenant_fk("memo_signature_samples"),
        _user_fk("memo_signature_samples", "user_id"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memo_signature_samples")),
    )
    op.create_index(
        "uq_memo_signature_samples_current",
        "memo_signature_samples",
        ["tenant_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "memo_signatures",
        *_id_and_tenant(),
        sa.Column("memo_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("response", sa.String(length=16), nullable=True),
        sa.Column("remark_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("method", sa.String(length=16), nullable=True),
        sa.Column("proof_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("sample_id", sa.Uuid(), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _tenant_fk("memo_signatures"),
        _user_fk("memo_signatures", "user_id"),
        sa.ForeignKeyConstraint(
            ["memo_id"],
            ["memos.id"],
            name=op.f("fk_memo_signatures_memo_id_memos"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["sample_id"],
            ["memo_signature_samples.id"],
            name=op.f("fk_memo_signatures_sample_id_memo_signature_samples"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memo_signatures")),
        sa.UniqueConstraint(
            "memo_id", "user_id", "role", name=op.f("uq_memo_signatures_memo_id_user_id_role")
        ),
    )
    op.create_index(op.f("ix_memo_signatures_memo_id"), "memo_signatures", ["memo_id"])
    op.create_index(op.f("ix_memo_signatures_user_id"), "memo_signatures", ["user_id"])
    op.create_index(op.f("ix_memo_signatures_sample_id"), "memo_signatures", ["sample_id"])

    op.create_table(
        "memo_views",
        *_id_and_tenant(),
        sa.Column("memo_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=False),
        _tenant_fk("memo_views"),
        _user_fk("memo_views", "user_id"),
        sa.ForeignKeyConstraint(
            ["memo_id"], ["memos.id"], name=op.f("fk_memo_views_memo_id_memos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memo_views")),
    )
    op.create_index(op.f("ix_memo_views_memo_id"), "memo_views", ["memo_id"])

    op.create_table(
        "memo_counters",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("last_number", sa.Integer(), nullable=False),
        _tenant_fk("memo_counters"),
        sa.PrimaryKeyConstraint("tenant_id", "kind", "year", name=op.f("pk_memo_counters")),
    )

    for table in _TABLES:
        if table != "memo_counters":
            op.create_index(op.f(f"ix_{table}_tenant_id"), table, ["tenant_id"])
        _apply_tenant_rls(table)


def downgrade() -> None:
    # Политики, индексы и FK удаляются вместе с таблицами; порядок — от зависимых.
    for table in reversed(_TABLES):
        op.drop_table(table)
