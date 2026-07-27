"""ORM-модели модуля guests (spec 0027 §1.1, ADR-008 §3, FOUNDATION §9).

Все пять таблиц тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграции 0014 (копия канона 0002).
`tenant_id` берётся из `tenant_context` по умолчанию; подлог чужого tenant_id
отвергает RLS-политика (WITH CHECK). Гость существует в границах тенанта —
кросс-отельного профиля нет (ADR-008, privacy).

Секреты в БД не хранятся в открытом виде (ADR-008):
- `StayAccessCode.code_hash` — bcrypt (пространство кодов мало́ — 30⁶,
  голый SHA-256 перебирается офлайн при утечке БД);
- `GuestSession.token_hash` — SHA-256 (энтропия токена 256 бит — медленный
  хэш не нужен, нужен индексируемый детерминизм для выборки по токену).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class GuestIdentityKind(enum.StrEnum):
    """Канал/источник идентификатора гостя (ADR-008 §3, GLOSSARY).

    В spec 0027 реально создаётся только `WEB` (привязка веб-чата по коду
    заселения); остальные значения — контракт модели: telegram появится с
    включением auth-only в Telegram, phone/email/pms — с соответствующими
    каналами и PMS-синхронизацией (ADR-004).
    """

    TELEGRAM = "telegram"
    WEB = "web"
    PHONE = "phone"
    EMAIL = "email"
    PMS = "pms"


class StayStatus(enum.StrEnum):
    """Жизненный цикл проживания (ADR-008 §3).

    Spec 0027 использует `checked_in`/`checked_out`; `expected` и `cancelled` —
    контракт под кабинет персонала (#48) и PMS-синхронизацию (ADR-004), веток
    кода под них в этой серии нет (P-1).
    """

    EXPECTED = "expected"
    CHECKED_IN = "checked_in"
    CHECKED_OUT = "checked_out"
    CANCELLED = "cancelled"


# Единственное место истины для enum-колонок: значения — .value членов;
# native_enum=False — обычный VARCHAR (смена состава значений — миграция
# данных, а не ALTER TYPE; тот же довод, что у RequestStatus).
_identity_kind_column_type = Enum(
    GuestIdentityKind,
    name="guest_identity_kind",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)
_stay_status_column_type = Enum(
    StayStatus,
    name="stay_status",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)


class Guest(Base):
    """Человек, проживающий или проживавший в отеле (GLOSSARY: «Гость»).

    Личность отделена от идентификаторов в каналах (`GuestIdentity`, §9):
    «гость = номер телефона» — запрещённое упрощение. `display_name` — PII
    (docs/PII_REGISTRY.md), необязательное: заселение не должно упираться
    в заполнение имени.
    """

    __tablename__ = "guests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    display_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class GuestIdentity(Base):
    """Идентификатор гостя в конкретном канале (§9, ADR-008 §3).

    Один гость — много идентификаторов; слияние дубликатов — перевешивание
    `guest_id` (модель поддерживает с первого дня, автоматика — не сейчас).
    Для `kind=web` `external_id` — UUID клиента, порождённый при привязке
    (у веба нет внешнего провайдера с готовым id); каждая успешная привязка
    (второе устройство, повторный ввод кода) — своя идентичность.
    """

    __tablename__ = "guest_identities"
    # Уникальность идентификатора — в границах тенанта (ADR-008 §3):
    # один и тот же telegram-аккаунт в двух отелях — два разных гостя.
    __table_args__ = (
        Index(
            "uq_guest_identities_tenant_kind_external",
            "tenant_id",
            "kind",
            "external_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    guest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guests.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[GuestIdentityKind] = mapped_column(_identity_kind_column_type)
    external_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class Stay(Base):
    """Проживание — источник истины про доступ гостя (GLOSSARY, ADR-008 §3).

    Код заселения и все гостевые сессии валидны, пока Stay в `checked_in` и
    `now < check_out_at`; продление/ранний выезд правят Stay — доступ следует
    автоматически, отдельного действия с токенами не существует.

    Время — UTC (§9); интерпретация «выезд в 12:00» — в часовом поясе отеля
    из конфига тенанта, это забота вызывающих (CLI/кабинет), не модели.
    """

    __tablename__ = "stays"
    # Один АКТИВНЫЙ Stay на комнату — свойство БД, а не только проверка кода
    # (рекомендация ревью spec 0027): второй параллельный check_in той же
    # комнаты отвергает индекс, а не гонка проверок.
    __table_args__ = (
        Index(
            "uq_stays_tenant_room_checked_in",
            "tenant_id",
            "room_number",
            unique=True,
            postgresql_where="status = 'checked_in'",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    guest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guests.id", ondelete="CASCADE"), index=True
    )
    room_number: Mapped[str] = mapped_column(String(20))
    status: Mapped[StayStatus] = mapped_column(_stay_status_column_type)
    check_in_at: Mapped[datetime] = mapped_column(UTCDateTime())
    check_out_at: Mapped[datetime] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class StayAccessCode(Base):
    """Per-stay код заселения — только bcrypt-хэш (ADR-008 §3, GLOSSARY).

    После показа при заселении код невосстановим; «гость потерял код» —
    перевыпуск (`reissue_access_code`): новый код гасит старый (`revoked_at`),
    существующие привязки и сессии продолжают жить. Собственного срока жизни
    у кода нет — валидность производна от Stay.
    """

    __tablename__ = "stay_access_codes"
    # Один активный код на Stay (ADR-008 §3): перевыпуск обязан сначала
    # погасить старый — двусмысленность «какой код действующий» исключена БД.
    __table_args__ = (
        Index(
            "uq_stay_access_codes_active_stay",
            "stay_id",
            unique=True,
            postgresql_where="revoked_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    stay_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stays.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(128))
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class GuestSession(Base):
    """Сессия гостевого канала после ввода кода (ADR-008 §3, GLOSSARY).

    Валидность перепроверяется НА КАЖДОМ действии (`service.resolve_session`):
    хэш найден, не отозвана, Stay в `checked_in`, `now < check_out_at` —
    «истёкшая сессия не может действовать» выполняется конструктивно (#79).
    Выезд (`check_out`) гасит все сессии Stay (Q8: grace-периода нет).

    `consent_at`/`consent_version` — согласие на обработку ПД, зафиксированное
    в момент привязки (юраудит 22.07, consent-gate вариант A): кнопка входа на
    странице совмещена с согласием, без него сессии (и вызовов LLM) нет.
    """

    __tablename__ = "guest_sessions"
    __table_args__ = (
        Index("uq_guest_sessions_tenant_token", "tenant_id", "token_hash", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    stay_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stays.id", ondelete="CASCADE"), index=True
    )
    guest_identity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("guest_identities.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 hex (64 символа) от opaque-токена ≥ 256 бит (см. докстринг модуля).
    token_hash: Mapped[str] = mapped_column(String(64))
    # Обновляется не чаще раза в несколько минут (не писать на каждый poll) —
    # наблюдаемость мёртвых сессий, как ApiKey.last_used_at (ADR-008 §2).
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consent_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    consent_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
