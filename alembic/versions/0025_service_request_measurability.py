"""service_requests: claimed_at, closed_at, closed_by_*, origin — измеримость пилота

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-22

Spec 0035 §3–§4 (issue #298, находка Б-10 операционного аудита 05.08.2026):
у заявки не было ни момента взятия, ни момента закрытия, ни признака того, чем
она была — обращением гостя или записью сотрудника. Exit-критерий Phase 1
(«≥ 70% заявок без участия ресепшена») этим не считался, а `claimed_at` задним
числом не восстанавливается ничем — поэтому колонки заводятся ДО старта пилота.

Четыре колонки времени и авторства NULLABLE (§3):

- `claimed_at` / `closed_at` — пишутся на КАЖДОМ переходе, из любого канала:
  медиана времени взятия, посчитанная только по кабинету, занизила бы объём
  работы, сделанной через Telegram (оба пути живут одновременно, spec 0033 §8).
- `closed_by_user_id` (FK на платформенных `users`, ondelete SET NULL — образец
  `claimed_by_user_id`, миграция 0018) и `closed_by_display_name` — снапшот
  «кто закрыл», пишется только когда личность известна (`acting_user` кабинета);
  Telegram-путь персонала идентичности не несёт (ADR-008 §7), гость — тем более.
  PII сотрудника — строка в docs/PII_REGISTRY.md в этом же PR.

`origin VARCHAR(16) NOT NULL` — канон enum-как-VARCHAR (как `status`, миграция
0006): состав значений меняется миграцией данных, а не `ALTER TYPE`.

Бэкфилл:

- `origin = 'guest_chat'` всем существующим строкам — не приближение, а факт:
  до этой спеки заявку создавал только AI-инструмент. Значение приезжает
  временным `server_default` (Postgres 11+ заполняет им колонку без переписи
  таблицы) и снимается сразу после — дальше INSERT без `origin` обязан падать
  громко, а не молча подмешиваться в один из двух измеряемых ярлыков (§4).
- `closed_at = updated_at` строкам в терминальном статусе, КРОМЕ уже
  обезличенных: у них `updated_at` — момент прогона ретеншна (#42), а не момент
  закрытия (тот же дефект, что чинил ревью PR #154). Литерал плейсхолдера
  вписан сюда строкой намеренно: миграция — снимок схемы и данных на свою дату,
  и переименование `REQUEST_TEXT_ANONYMIZED_PLACEHOLDER`
  (`modules/requests/service.py`) не должно задним числом менять её смысл.
- `claimed_at` не бэкфиллится ничем: его неоткуда взять, а приблизительное
  значение попало бы прямо в метрику. Цена честного NULL — медиана времени
  взятия не считается по заявкам старше миграции; на пилоте таких нет.

**Принятый риск: этот шаг не обратно-совместим, в отличие от соседних.**
`deploy.sh` гоняет `alembic upgrade head` ДО пересоздания контейнеров, поэтому
в окне деплоя (миграция + сид, десятки секунд) старое приложение работает на
новой схеме — а его INSERT без `origin` теперь падает. То же и после отката на
образ до #298: схему `deploy.sh` не опускает, и старый код перестанет создавать
заявки, пока не выполнить `alembic downgrade 0024` (симптом и команда — в
`docs/runbooks/deploy.md`, части C и D). Риск принят осознанно: альтернатива —
оставить `server_default` до следующего релиза, но тогда второй щит §4 на этот
релиз отсутствует, и путь, забывший назвать источник, весь пилотный день молча
писал бы `guest_chat` — примесь прямо в измеряемое число, невосстановимую
задним числом. Соседняя 0024 выбрала обратное и по другой причине: у `is_urgent`
умолчание `false` — честный ответ для любой вставки, у `origin` честного
умолчания не существует.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_requests", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "service_requests", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("service_requests", sa.Column("closed_by_user_id", sa.Uuid(), nullable=True))
    op.add_column(
        "service_requests",
        sa.Column("closed_by_display_name", sa.String(length=255), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_service_requests_closed_by_user_id_users"),
        "service_requests",
        "users",
        ["closed_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "service_requests",
        sa.Column("origin", sa.String(length=16), nullable=False, server_default="guest_chat"),
    )
    # `service_requests` под FORCE ROW LEVEL SECURITY (миграция 0006, канон 0002),
    # а GUC `app.tenant_id` миграции не ставят: под ролью-владельцем без BYPASSRLS
    # политика отфильтровала бы ВСЕ строки, и UPDATE обновил бы ноль — молча.
    # `row_security = off` делает этот отказ громким: суперпользователю (нынешняя
    # роль миграций) это no-op, любой другой роли — ошибка вместо тишины.
    op.execute("SET LOCAL row_security = off")
    # Литерал — снимок REQUEST_TEXT_ANONYMIZED_PLACEHOLDER на дату миграции
    # (см. докстринг), канон подстановки значений данных — миграция 0011.
    op.execute(
        """
        UPDATE service_requests
           SET closed_at = updated_at
         WHERE status IN ('done', 'cancelled')
           AND summary <> '[обезличено: срок хранения истёк]'
        """
    )
    # Вернуть RLS: `alembic upgrade` держит все шаги в одной транзакции, и без
    # этой строки следующие миграции исполнялись бы с выключенной политикой —
    # действие на расстоянии, которого никто не заказывал.
    op.execute("SET LOCAL row_security = on")
    # Умолчание было нужно ровно для бэкфилла (§4: второй щит обязательности —
    # сама БД, INSERT без origin падает громко).
    op.alter_column("service_requests", "origin", server_default=None)


def downgrade() -> None:
    op.drop_column("service_requests", "origin")
    op.drop_constraint(
        op.f("fk_service_requests_closed_by_user_id_users"),
        "service_requests",
        type_="foreignkey",
    )
    op.drop_column("service_requests", "closed_by_display_name")
    op.drop_column("service_requests", "closed_by_user_id")
    op.drop_column("service_requests", "closed_at")
    op.drop_column("service_requests", "claimed_at")
