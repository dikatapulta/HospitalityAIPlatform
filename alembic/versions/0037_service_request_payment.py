"""service_requests: отметка оплаты — payment_required, paid_at, paid_by_*

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-28

Spec 0038 Р-2 и §3 (issue #418, PR 2 серии): заказ в номер — заявка, которую
нельзя закрыть «Готово», пока сотрудник не отметил, что деньги у него.
«Оплачено» — отметка на заявке, а не статус: новый статус пришлось бы учить
очередь, сводку, лестницу и журнал, а это факт о деньгах, не стадия работы.

- `payment_required` NOT NULL с `server_default false`: у существующих заявок
  оплаты нет, умолчание остаётся насовсем — честное значение для вставок мимо
  ORM (тот же довод, что у `is_urgent`, миграция 0024).
- `paid_at` + `paid_by_user_id` (FK `users` ON DELETE SET NULL) +
  `paid_by_display_name` — зеркало пары `claimed_*` (миграции 0018, 0025).
  Имя — PII сотрудника, строка в docs/PII_REGISTRY.md.
- CHECK `paid_at IS NULL OR payment_required`: отметка без признака — ошибка
  данных, а не состояние.

Бэкфилла нет: новые колонки пусты или равны умолчанию у всех строк.

Откат: колонки снимаются вместе с отметками. До PR 5 серии (страница заказа)
ни одна заявка признака не получает, так что терять нечего; после — откат
стирает, кто и когда отметил оплату, а заявки с `origin = 'guest_form'` старый
код не прочтёт (значения нет в его enum) — откат за PR 5 не делается без
миграции данных.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_requests",
        sa.Column("payment_required", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "service_requests", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("service_requests", sa.Column("paid_by_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "service_requests",
        sa.Column("paid_by_display_name", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_service_requests_paid_by_user_id_users"),
        "service_requests",
        "users",
        ["paid_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_service_requests_paid_requires_payment"),
        "service_requests",
        "paid_at IS NULL OR payment_required",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_service_requests_paid_requires_payment"), "service_requests", type_="check"
    )
    op.drop_constraint(
        op.f("fk_service_requests_paid_by_user_id_users"),
        "service_requests",
        type_="foreignkey",
    )
    op.drop_column("service_requests", "paid_by_display_name")
    op.drop_column("service_requests", "paid_by_user_id")
    op.drop_column("service_requests", "paid_at")
    op.drop_column("service_requests", "payment_required")
