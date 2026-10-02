"""platform: вход персонала по логину отеля — staff_invites.login, email уходит

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-02

Spec 0037 §6 (issue #368), ревизия ADR-008 §1 от 27.09.2026: сотрудник входит
не по email, а по короткому логину отеля (`BORM`), который задаёт менеджер в
приглашении. Схема `user_identities` не меняется — у `kind=password`
`external_id` становится `<tenant_id>:<LOGIN>`, и уникальность логина внутри
отеля даёт уже существующий UNIQUE `(kind, external_id)`.

Три шага:

1. `staff_invites.login VARCHAR(12)` — nullable только ради старых строк: новые
   приглашения без логина не выпускаются.
2. Ожидающие приглашения без логина отзываются (`expires_at = now()`, канон
   `revoke_invite`) — принять их нечем. Код на этот шаг не полагается: строку
   с `login IS NULL` он сам считает мёртвой.
3. **Удаляются** email-идентичности: `kind = 'password'` и `@` в `external_id`.
   Цель задачи — не хранить email, а входить по нему новый код всё равно не
   умеет. Условие по формату, а не «все `password`»: повторный `upgrade` после
   `downgrade` не сносит уже выданные логины (в `<tenant_id>:<LOGIN>` нет `@`).

Пользователи и членства не трогаются: история «кто взял / кто закрыл»
остаётся, живые сессии доживают свой срок (переход живой учётки — spec 0037 §6).

`downgrade` возвращает только схему: email восстановить неоткуда, и это
осознанно. Откат образа старше этой миграции — отдельный рецепт
(`docs/runbooks/deploy.md`, часть C): старый код ищет email, которых больше
нет, и в кабинет не войдёт никто.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("staff_invites", sa.Column("login", sa.String(length=12), nullable=True))
    op.execute(
        "UPDATE staff_invites SET expires_at = now()"
        " WHERE login IS NULL AND accepted_at IS NULL AND expires_at > now()"
    )
    op.execute(
        "DELETE FROM user_identities WHERE kind = 'password' AND position('@' in external_id) > 0"
    )


def downgrade() -> None:
    op.drop_column("staff_invites", "login")
