"""ORM-модели модуля platform (Task 0008/0009, FOUNDATION §9, ADR-003, ADR-008).

`Tenant` — корень мультитенантности: единица изоляции данных и конфигурации
(GLOSSARY: «Тенант»). Сама таблица `tenants` — НЕ тенантная (это реестр
тенантов), поэтому `tenant_id` и RLS на ней нет.

`TenantIsolationCanary` — канонический образец тенантной таблицы (Task 0009):
новые тенантные модели копируют её паттерн, а миграции — RLS-блок из
`alembic/versions/0002_tenant_rls_canon.py`.

Staff-идентичность (spec 0033 §3.1, ADR-008 §1) — платформенный мир ВНЕ RLS,
как `tenants`: эти таблицы устанавливают контекст тенанта и работают до/поверх
него (login, membership many-to-many, platform_admin). Компенсация — доступ
только из кода `platform/` (граница R-5). `TenantMembership` и `StaffInvite`
несут `tenant_id` без RLS — whitelist-исключение из P-4 (ADR-008 §6), список —
в docstring миграции 0017.

Секреты — только хэши (ADR-008, канон guests/models.py):
- `UserIdentity.secret_hash` — argon2id (пароль выбирает человек — медленный
  memory-hard хэш обязателен);
- `StaffSession.token_hash` / `StaffInvite.token_hash` — SHA-256 (энтропия
  токена 256 бит; нужен индексируемый детерминизм для выборки по токену).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Enum, ForeignKey, Index, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    # slug — стабильный человекочитаемый идентификатор (URL, конфиги, сиды);
    # name — отображаемое название, может меняться свободно.
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    # Конфигурация тенанта (§6, Task 0011): форму задаёт схема TenantConfig,
    # читать/писать только через load_tenant_config/store_tenant_config
    # (platform/config.py). NULL = онбординг не завершён.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB(), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class TenantIsolationCanary(Base):
    """CANONICAL: образец тенантной таблицы (Task 0009, P-4, ADR-003).

    Вечный якорь обязательного теста изоляции (`tests/test_tenant_isolation.py`,
    отдельный блокирующий шаг CI) и образец для копирования в каждую новую
    тенантную таблицу. В проде пуста — бизнес-данных не несёт.

    Канон тенантной модели:
    - `tenant_id` NOT NULL с FK на `tenants.id` и индексом;
    - default берёт тенанта из `tenant_context` — забыть проставить нельзя,
      а подлог чужого tenant_id всё равно отвергает RLS-политика (WITH CHECK);
    - в миграции таблица получает RLS-блок (ENABLE + FORCE + политика) —
      см. канонический комментарий в миграции 0002.
    """

    __tablename__ = "tenant_isolation_canary"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    note: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


# ---------------------------------------------------------------------------
# Staff-идентичность (spec 0033 §3.1, ADR-008 §1) — платформенный мир вне RLS.
# ---------------------------------------------------------------------------


class UserStatus(enum.StrEnum):
    """Статус личности сотрудника (ADR-008 §1).

    Деактивация — одно действие: вход закрыт, сессии погашены, членства
    отозваны, аудит-запись остаётся (`staff_auth.deactivate_user`). Удаления
    User в v1 нет — история действий должна оставаться атрибутируемой.
    """

    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class UserIdentityKind(enum.StrEnum):
    """Способ входа сотрудника (ADR-008 §1, инвариант б).

    В v1 реально создаётся только `PASSWORD`; `OIDC` и `TELEGRAM` —
    зарезервированные значения контракта, веток кода нет (P-1): SSO — новый
    kind и шаг login-flow, привязка Telegram сотрудника — follow-up после
    серии 0033.
    """

    PASSWORD = "password"
    OIDC = "oidc"
    TELEGRAM = "telegram"


class MembershipStatus(enum.StrEnum):
    """Статус членства сотрудника в тенанте (ADR-008 §1)."""

    ACTIVE = "active"
    REVOKED = "revoked"


class StaffRole(enum.StrEnum):
    """Роли v1 — минимум, совместимый с RBAC v1 (spec 0033 §3.2).

    Полная матрица роль × действие — `docs/RBAC.md` (задача RBAC v1, PR F
    серии 0033 даёт мини-матрицу). Роль живёт на членстве и одна на членство
    (ADR-008 §1); новая комбинация прав — новая роль здесь, а не вторая
    строка членства.
    """

    STAFF = "staff"
    RECEPTIONIST = "receptionist"
    MANAGER = "manager"


# Единственное место истины для enum-колонок staff-мира: значения — .value
# членов; native_enum=False — обычный VARCHAR без CHECK (расширение состава
# значений, например ролей в RBAC v1, — правка кода, а не ALTER TYPE; тот же
# довод, что у RequestStatus и StayStatus).
_user_status_column_type = Enum(
    UserStatus,
    name="user_status",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)
_user_identity_kind_column_type = Enum(
    UserIdentityKind,
    name="user_identity_kind",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)
_membership_status_column_type = Enum(
    MembershipStatus,
    name="membership_status",
    native_enum=False,
    length=16,
    values_callable=lambda members: [member.value for member in members],
)
_staff_role_column_type = Enum(
    StaffRole,
    name="staff_role_key",
    native_enum=False,
    length=32,
    values_callable=lambda members: [member.value for member in members],
)


class User(Base):
    """Личность сотрудника отеля или платформы (ADR-008 §1, GLOSSARY).

    Платформенная сущность БЕЗ `tenant_id` (инвариант а: сети отелей и
    `platform_admin` несовместимы с полем тенанта у пользователя); связь с
    тенантами — только через `TenantMembership`. Гость — НЕ User (GLOSSARY).
    `display_name` — PII (docs/PII_REGISTRY.md). `is_platform_admin` — флаг
    оператора платформы вне тенантов, а не членство (ADR-008 §1).
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    display_name: Mapped[str] = mapped_column(String(255))
    status: Mapped[UserStatus] = mapped_column(_user_status_column_type, default=UserStatus.ACTIVE)
    is_platform_admin: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class UserIdentity(Base):
    """Способ входа сотрудника — зеркало `GuestIdentity` (ADR-008 §1, инвариант б).

    Для `kind=password` `external_id` — нормализованный email
    (`staff_auth.normalize_email`: trim + lowercase), `secret_hash` — argon2id;
    plaintext пароля в БД не попадает никогда. Один User — много способов
    входа; добавление/удаление способа не трогает User. Уникальность
    `(kind, external_id)` — на всю платформу (email принадлежит одному User;
    сеть отелей — одна личность, много членств).
    """

    __tablename__ = "user_identities"
    __table_args__ = (
        Index("uq_user_identities_kind_external", "kind", "external_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[UserIdentityKind] = mapped_column(_user_identity_kind_column_type)
    external_id: Mapped[str] = mapped_column(String(255))
    secret_hash: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class TenantMembership(Base):
    """Членство сотрудника в тенанте — здесь живёт роль (ADR-008 §1).

    `tenant_id` БЕЗ RLS — whitelist-исключение из P-4 (ADR-008 §6, docstring
    миграции 0017): членство проверяется до установки контекста тенанта.
    Одна роль на членство; отзыв членства закрывает доступ к тенанту на
    следующей же проверке запроса (`staff_auth.require_role`), сессии User
    при этом продолжают жить — они принадлежат личности, не членству.
    """

    __tablename__ = "tenant_memberships"
    __table_args__ = (
        Index("uq_tenant_memberships_user_tenant", "user_id", "tenant_id", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    role_key: Mapped[StaffRole] = mapped_column(_staff_role_column_type)
    status: Mapped[MembershipStatus] = mapped_column(
        _membership_status_column_type, default=MembershipStatus.ACTIVE
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class StaffSession(Base):
    """Серверная сессия кабинета персонала (ADR-008 §1, spec 0033 §3.3).

    Названа `staff_sessions` (не `sessions`) — симметрия с `guest_sessions`.
    Принадлежит User, а не членству: активный тенант — атрибут запроса
    (slug в пути кабинета), membership проверяется на каждом запросе.

    `expires_at` — absolute-предел (создание + STAFF_SESSION_ABSOLUTE_TTL_DAYS);
    idle-предел проверяется по `last_used_at` (+ STAFF_SESSION_IDLE_TTL_DAYS),
    активность его продлевает (запись — не чаще раза в несколько минут, канон
    `GuestSession.last_used_at`). Отвергнуто — stateless JWT (ADR-008 §1):
    мгновенный отзыв при деактивации всё равно требует похода в хранилище.
    """

    __tablename__ = "staff_sessions"
    __table_args__ = (Index("uq_staff_sessions_token_hash", "token_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # SHA-256 hex (64 символа) от opaque-токена ≥ 256 бит (канон GuestSession).
    token_hash: Mapped[str] = mapped_column(String(64))
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class StaffInvite(Base):
    """Одноразовое приглашение сотрудника (spec 0033 §3.4, ADR-008 §1).

    Provisioning-артефакт, НЕ способ входа: по принятии создаёт User +
    `UserIdentity(password)` + membership (существующий email — только
    membership, сеть отелей). `tenant_id` без RLS — то же whitelist-исключение
    P-4, что у `TenantMembership`. Ссылку менеджер передаёт сам (WhatsApp/
    лично) — email-порт не нужен. Отзыв/перевыпуск — `expires_at = now`
    (отдельной колонки revoked_at нет: погашенный и истёкший инвайты
    неразличимы и для гостя ссылки, и для UI — «попросите новое приглашение»).
    """

    __tablename__ = "staff_invites"
    __table_args__ = (Index("uq_staff_invites_token_hash", "token_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    role_key: Mapped[StaffRole] = mapped_column(_staff_role_column_type)
    # Имя приглашаемого — PII (docs/PII_REGISTRY.md): становится display_name
    # созданного User; у существующего User имя не перезаписывается.
    invited_name: Mapped[str] = mapped_column(String(255))
    # SHA-256 hex от одноразового токена ссылки (в ссылке — plaintext, в БД — хэш).
    token_hash: Mapped[str] = mapped_column(String(64))
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
