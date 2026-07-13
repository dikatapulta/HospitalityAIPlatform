"""ORM-модели канала Telegram: Conversation, Message (Task 0016, §9, ADR-003).

Обе таблицы тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`, Task 0009), RLS-блок — в миграции 0008 (копия канона
0002). `tenant_id` берётся из `tenant_context` по умолчанию; подлог чужого
tenant_id отвергает RLS-политика (WITH CHECK).

Phase 0: гость — просто строка `Conversation` (модуля `guests/` ещё нет,
см. PHASE0 «чего нет»). Идентичность гостя (`Guest`/`GuestIdentity`, §9)
появится в Phase 1 — отдельными таблицами, без переделки этих.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class MessageDirection(enum.StrEnum):
    """Направление сообщения: входящее от гостя или исходящее от платформы."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageContentKind(enum.StrEnum):
    """Тип содержимого сохранённого сообщения (совпадает с channels.base.MessageKind).

    Хранится строкой, чтобы история диалога была самодостаточна: по ней видно,
    что гость прислал не-текст, даже если сам контент Phase 0 не разбирает.
    """

    TEXT = "text"
    UNSUPPORTED = "unsupported"


# Единственное место истины для enum-колонок: значения — .value членов
# (SQLAlchemy по умолчанию пишет ИМЕНА — "INBOUND"; нам нужны "inbound").
# native_enum=False — обычный VARCHAR: смена состава значений остаётся миграцией
# данных, а не ALTER TYPE (тот же довод, что у RequestStatus в модуле requests).
_direction_column_type = Enum(
    MessageDirection,
    name="message_direction",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)
_content_kind_column_type = Enum(
    MessageContentKind,
    name="message_content_kind",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)


class Conversation(Base):
    """Диалог с гостем в конкретном канале (§9: сущность Conversation).

    Один диалог на пару (канал, чат гостя) у тенанта: сообщения гостя из одного
    Telegram-чата собираются в одну Conversation. `external_id` — идентификатор
    чата у провайдера (Telegram chat.id как строка).
    """

    __tablename__ = "conversations"
    # Один диалог на (tenant, channel, external_id): повторное сообщение из того
    # же чата находит существующий диалог, а не плодит новый.
    __table_args__ = (UniqueConstraint("tenant_id", "channel", "external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    channel: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    # Состояние гейта подтверждения P-9 между ходами (Task 0017): предложенный, но
    # не исполненный вызов инструмента (сериализованный `ai.orchestrator.
    # PendingAction`: {"tool_name", "arguments"}). NULL = ожидания нет. Гейт должен
    # пережить два вебхука (гость просит → «оформить?» → гость «да»), поэтому живёт
    # в БД рядом с диалогом, а не в памяти процесса (ADR-011).
    pending_action: Mapped[dict[str, Any] | None] = mapped_column(JSONB())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class Message(Base):
    """Одно сообщение диалога (§9: сущность Message; таблица с retention — Phase 1).

    Входящее несёт `idempotency_key` (ключ доставки провайдера) под уникальным
    ограничением — повторный вебхук не создаёт второго сообщения (P-8). Исходящее
    `idempotency_key` не имеет (NULL): у платформенных ответов нет внешней доставки,
    которую надо дедуплицировать, а Postgres считает NULL-и различными.
    """

    __tablename__ = "messages"
    # Идемпотентность входящих (P-8): ключ доставки уникален в пределах тенанта.
    # Namespace в ключе ("telegram:update:<id>") исключает коллизию между каналами,
    # поэтому канал в ограничение не входит. NULL (исходящие) не участвуют.
    __table_args__ = (UniqueConstraint("tenant_id", "idempotency_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(_direction_column_type)
    content_kind: Mapped[MessageContentKind] = mapped_column(_content_kind_column_type)
    # Текст сообщения; NULL для не-текстовых входящих (content_kind=unsupported).
    text: Mapped[str | None] = mapped_column(Text())
    # Идентификатор сообщения у провайдера (Telegram message_id как строка):
    # для входящих — по нему Phase 1 восстановит reply_to; для исходящих —
    # id отправленного сообщения (если провайдер вернул).
    external_message_id: Mapped[str | None] = mapped_column(String(128))
    # Ключ идемпотентности доставки (см. __table_args__); NULL у исходящих.
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    # correlation_id запроса-вебхука (§10.2): связывает строку с её следом в логах —
    # прямая опора DoD «Message в БД с correlation_id».
    correlation_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class RequestOrigin(Base):
    """Привязка заявки к диалогу-источнику (Task 0017, ADR-011): куда вернуть гостю
    подтверждение о выполнении.

    Событие `request.status_changed` несёт только доменный `request_id` — оно не
    знает канал и чат гостя (в Phase 0 нет модуля `guests/` и идентичностей). Канал
    записывает эту привязку в момент создания заявки (`ACTION_DONE` оркестратора) и
    по ней подписчик `notify_guest_on_request_done` находит чат гостя. Обратная
    адресация — забота композиционного слоя, а не домена (P-2/P-5).

    `request_id` — БЕЗ FK на `service_requests`: канал не связывает свою схему с
    таблицей чужого модуля (P-2), хранит id как непрозрачную ссылку из события.
    Таблица тенантная (RLS-канон 0002). В Phase 1 вытесняется резолвом идентичности
    гостя (`guests/` + `GuestIdentity`) — тогда ADR-011 помечается superseded.
    """

    __tablename__ = "request_origins"
    # Одна привязка на заявку у тенанта: повторная запись того же request_id
    # (пере-доставка/ретрай) идемпотентна — конфликт по этому ограничению.
    __table_args__ = (UniqueConstraint("tenant_id", "request_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    # Доменный id заявки (из события request.created) — непрозрачная ссылка, без FK.
    request_id: Mapped[uuid.UUID] = mapped_column(Uuid())
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
