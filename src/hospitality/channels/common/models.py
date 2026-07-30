"""ORM-модели диалога — общие для всех гостевых каналов (Task 0016, spec 0027 §2, §9).

Таблицы канал-агностичны с рождения (колонка `channel`, уникальность
`(tenant_id, channel, external_id)`): §9 объявляет Conversation/Message
сущностями ядра диалога, а не Telegram. До spec 0027 код жил в
`channels/telegram/models.py`; вынесен сюда с приходом второго канала (web) —
таблицы и миграции (0008/0009) не менялись, только модуль-владелец.

Все таблицы тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`, Task 0009), RLS-блок — в миграции 0008 (копия канона
0002). `tenant_id` берётся из `tenant_context` по умолчанию; подлог чужого
tenant_id отвергает RLS-политика (WITH CHECK).

Идентичность гостя (`Guest`/`GuestIdentity`) живёт в `modules/guests`
(spec 0027); связь `Conversation` → `GuestIdentity` появится аддитивной
колонкой в PR D (веб-канал), телеграм-диалоги остаются без неё до auth-only.
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

    Состав обязан отражать `channels.base.MessageKind`: `insert_inbound_message`
    пишет `MessageContentKind(message.kind.value)`, поэтому недостающее значение
    роняет запись входящего (spec 0021 П-2: `callback` — нажатие inline-кнопки).
    Колонка — VARCHAR(16), `native_enum=False` (см. ниже), так что новое значение
    не требует миграции БД, пока укладывается в длину.
    """

    TEXT = "text"
    # Нажатие inline-кнопки персонала (callback_query): `text` = callback_data
    # (`req:<uuid>:<действие>`), а не свободный текст (spec 0021 П-2).
    CALLBACK = "callback"
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
    # Идентичность гостя (modules/guests, spec 0027 §3.2, миграция 0015):
    # веб-диалог рождается привязанным (заполняет канал web), telegram — NULL до
    # auth-only. БЕЗ FK — граница модулей (довод request_origins.request_id).
    guest_identity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    # Согласие гостя на обработку ПД (spec 0029 §1, миграция 0016): доказательная
    # запись consent-gate'а канала telegram — у телеграм-гостя нет ни Stay, ни
    # сессии, и диалог остаётся единственной его долгоживущей сущностью.
    # NULL = согласия нет (все диалоги до миграции) → гейт при первом сообщении.
    # Web хранит своё согласие на сессии (`guest_sessions`, spec 0027): там оно
    # даётся на каждую привязку. Правило «версия актуальна» — одно на оба канала
    # (`channels/common/consent.py`).
    consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consent_version: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class Message(Base):
    """Одно сообщение диалога (§9: сущность Message).

    Retention (§9, issue #42, spec 0032): строки старше `messages_retention_days`
    удаляет `channels/common/retention.py` из цикла воркера; опустевшие давно не
    обновлявшиеся Conversation удаляются следом.

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
    # Текст сообщения; у CALLBACK — callback_data кнопки (`req:<uuid>:<действие>`).
    # NULL для не-текстовых входящих (content_kind=unsupported): фото/стикер/голос.
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
    по ней подписчик `notify_guest_on_request_closed` находит диалог гостя. Обратная
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
