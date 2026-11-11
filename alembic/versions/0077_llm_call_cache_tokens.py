"""ai/gateway: токены кэша промпта в журнале вызовов

Revision ID: 0077
Revises: 0075
Create Date: 2026-11-11

Issue #138: гостевой ход отмечает стабильную часть системной инструкции точкой
кэша Anthropic, и вход вызова делится на три цены — обычную, запись в кэш
(1,25×) и чтение из кэша (0,1×). Без этих колонок журнал не показывает, что кэш
работает, а стоимость строки нельзя сверить с токенами.

- `cache_creation_input_tokens`, `cache_read_input_tokens` — `INTEGER NOT NULL
  DEFAULT 0`. Это доли `input_tokens`, а не добавка к нему: `input_tokens`
  по-прежнему весь вход вызова. Существующие строки получают 0 умолчанием
  самого ADD COLUMN — до этой миграции кэша не было, бэкфилла нет.
- `server_default` остаётся насовсем: старый образ в окне деплоя (и при откате)
  вставляет строки без этих колонок (канон 0040).

Откат снимает колонки: стоимость уже записанных строк остаётся верной, теряется
только разбивка входа.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0075"
branch_labels = None
depends_on = None

_COLUMNS = ("cache_creation_input_tokens", "cache_read_input_tokens")


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(
            "llm_call_log",
            sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    for column in _COLUMNS:
        op.drop_column("llm_call_log", column)
