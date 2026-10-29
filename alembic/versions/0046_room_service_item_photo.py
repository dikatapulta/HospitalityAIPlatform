"""room_service: фото товара — ссылка на файл узла хранения

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-29

Spec 0038 §3 и §6 п. 7 (issue #418, PR 7 серии): у товара появляется фото —
строка `uploaded_files` (spec 0047, назначение `menu_photo`). Колонка
`room_service_items.photo_file_id`:

- nullable — у товара фото может не быть;
- FK на `uploaded_files.id` ON DELETE SET NULL: удалили файл — товар остаётся
  без фото, а не со ссылкой в никуда (FK в таблицу ядра, не в чужой модуль);
- UNIQUE — один файл у одного товара: замена удаляет прежний файл, и удаление
  не заденет фото соседа. Уникальность даёт и индекс выдачи фото по id файла.

RLS у таблицы уже есть (миграция 0041), колонка её не меняет. Шаг
обратно-совместим: старый образ колонку не читает. Downgrade снимает колонку,
а файлы фото остаются строками `uploaded_files` без ссылки: ретеншн
`menu_photo` не трогает (spec 0047 §6) — после отката ниже 0046 их удаляют руками.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_ITEMS = "room_service_items"


def upgrade() -> None:
    op.add_column(_ITEMS, sa.Column("photo_file_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f(f"fk_{_ITEMS}_photo_file_id_uploaded_files"),
        _ITEMS,
        "uploaded_files",
        ["photo_file_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(op.f(f"uq_{_ITEMS}_photo_file_id"), _ITEMS, ["photo_file_id"])


def downgrade() -> None:
    op.drop_constraint(op.f(f"uq_{_ITEMS}_photo_file_id"), _ITEMS, type_="unique")
    op.drop_constraint(
        op.f(f"fk_{_ITEMS}_photo_file_id_uploaded_files"), _ITEMS, type_="foreignkey"
    )
    op.drop_column(_ITEMS, "photo_file_id")
