"""channels/common: conversation_escalations.request_id — эскалация знает свою заявку

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-13

Spec 0039 §2.5 (issue #292): эскалация у отеля с заданной службой эскалаций
заводит заявку, и строка эскалации хранит ссылку на неё. По ссылке ход гостя
решает дедупликацию: несрочная эскалация не заводит новую карточку, пока у
диалога есть невзятая эскалационная (иначе десять сообщений гостя при лежащей
модели дали бы десять карточек).

`request_id UUID NULL` без внешнего ключа — непрозрачная доменная ссылка на
таблицу чужого модуля, тот же довод, что у `request_origins.request_id`. NULL —
заявки нет: служба эскалаций не задана, создание не удалось, эскалация
поглощена дедупликацией или записана до этой миграции. Индекса нет: поиск идёт
по уже проиндексированному `conversation_id`. Бэкфилла нет: у старых эскалаций
заявки не было. Персональных данных в колонке нет (spec 0035 §12 не меняется).

Шаг обратно-совместим: новая nullable-колонка, старый образ в окне деплоя её
не видит и пишет строки без неё.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_escalations", sa.Column("request_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation_escalations", "request_id")
