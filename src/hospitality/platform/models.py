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

`StaffActionLog` — журнал изменений кабинета (spec 0041): тенантная таблица
по канону `TenantIsolationCanary`, живёт в ядре, потому что действия рождаются
в двух модулях (`requests`, `guests`), а соседей они не импортируют. Функции —
`platform/staff_actions.py`.

Секреты — только хэши (ADR-008, канон guests/models.py):
- `UserIdentity.secret_hash` — argon2id (пароль выбирает человек — медленный
  memory-hard хэш обязателен);
- `StaffSession.token_hash` / `StaffInvite.token_hash` /
  `StaffPasswordReset.token_hash` — SHA-256 (энтропия токена 256 бит; нужен
  индексируемый детерминизм для выборки по токену).
Исключение — `StaffSecondFactor.totp_secret`: секрет кода из приложения
хэшировать нельзя, код сервер считает сам (spec 0044 §7).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Uuid,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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

    `ERASED` — «стёрта суперадмином» (spec 0044 §5): то же, что отключена, плюс
    учётки нет в «Сотрудниках» и стёрт хэш пароля. Код проверяет «не `ACTIVE`»,
    поэтому стёртая закрыта везде, где закрыта отключённая.
    """

    ACTIVE = "active"
    DEACTIVATED = "deactivated"
    ERASED = "erased"


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
    """Уровень сотрудника в отеле — одна из двух настроек членства (spec 0045 §2).

    Вторая настройка — службы (`TenantMembership.service_keys`): какие заявки
    человек ведёт. Матрица уровень × действие — `docs/RBAC.md`. Роль живёт на
    членстве и одна на членство (ADR-008 §1). Роли на службу нет (NG-7):
    ресепшен — это служба с ключом `staff_auth.RECEPTION_SERVICE_KEY`, а не роль;
    прежняя роль `receptionist` переведена миграцией 0033 в `staff` + ресепшен.
    """

    STAFF = "staff"
    SENIOR = "senior"
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
# Ключ службы = `request_categories.key` (String(63) там же).
_service_keys_column_type = ARRAY(String(63))


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

    Для `kind=password` `external_id` — `<tenant_id>:<LOGIN>`, логин отеля
    (spec 0037 §6; собирает и разбирает строку только пара функций
    `staff_credentials.password_external_id`/`parse_password_external_id`),
    `secret_hash` — argon2id; plaintext пароля в БД не попадает никогда. Один
    User — много способов входа; добавление/удаление способа не трогает User.
    Уникальность `(kind, external_id)` — на всю платформу, и для пароля она
    же даёт уникальность логина внутри отеля без колонки `tenant_id`.
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
    # Службы — ключи категорий заявок отеля (spec 0045 §2): строками, потому что
    # FK из платформенного мира на тенантную таблицу под RLS не ставится. У
    # менеджера пусто. Умолчание в БД — ради старого образа при откате.
    service_keys: Mapped[list[str]] = mapped_column(
        _service_keys_column_type, default=list, server_default=text("'{}'")
    )
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

    Provisioning-артефакт, НЕ способ входа: по принятии всегда создаёт нового
    User + `UserIdentity(password)` с логином из приглашения + membership
    (spec 0037 §4). `tenant_id` без RLS — то же whitelist-исключение
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
    # Службы будущего членства (spec 0045 §6) — переносятся в него при принятии.
    service_keys: Mapped[list[str]] = mapped_column(
        _service_keys_column_type, default=list, server_default=text("'{}'")
    )
    # Имя приглашаемого — PII (docs/PII_REGISTRY.md): становится display_name
    # созданного User.
    invited_name: Mapped[str] = mapped_column(String(255))
    # Логин сотрудника, заданный менеджером (spec 0037 §4) — PII. NULL только у
    # строк до миграции 0028 и от старого образа при откате: такое приглашение
    # мёртвое, как истёкшее (`staff_invites._pending`).
    login: Mapped[str | None] = mapped_column(String(12))
    # SHA-256 hex от одноразового токена ссылки (в ссылке — plaintext, в БД — хэш).
    token_hash: Mapped[str] = mapped_column(String(64))
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class StaffSecondFactor(Base):
    """Второй фактор суперадмина — секрет кода из приложения (spec 0044 §3, §7).

    Строка есть только у учётки с `is_platform_admin`: без неё суперадмин не
    войдёт вовсе (fail-closed). `totp_secret` — base32, открытым: хэшировать
    нельзя, код считает сервер (`platform/totp.py`); почему не шифруется —
    spec 0044 §7. `last_used_step` — шаг последнего принятого кода: код того
    же или более раннего шага — повтор, отказ. Платформенная таблица без
    `tenant_id` и RLS, как `users`.
    """

    __tablename__ = "staff_second_factors"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    totp_secret: Mapped[str] = mapped_column(String(64))
    last_used_step: Mapped[int | None] = mapped_column(BigInteger())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class StaffPasswordReset(Base):
    """Одноразовая ссылка нового пароля, выданная суперадмином (spec 0044 §4).

    Привязана к идентичности `kind=password`, а не к отелю: отель виден из её
    `external_id`, поэтому `tenant_id` здесь нет и нового исключения P-4 тоже.
    Погашенная (новой ссылкой, стиранием) и истёкшая неразличимы —
    `expires_at = now`, как у `StaffInvite`. В БД только SHA-256 токена.
    """

    __tablename__ = "staff_password_resets"
    __table_args__ = (Index("uq_staff_password_resets_token_hash", "token_hash", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    identity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_identities.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64))
    issued_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


# ---------------------------------------------------------------------------
# Журнал изменений кабинета (spec 0041) — тенантная таблица, канон RLS.
# ---------------------------------------------------------------------------


class StaffAction(enum.StrEnum):
    """Что сделал сотрудник — строка журнала изменений (spec 0041 §2.2).

    Подписи для менеджера живут на странице (`staff_portal/changes.py`), здесь
    только значения колонки. VARCHAR без CHECK (довод `StaffRole`): новое
    действие (отметка оплаты, spec 0038 §7) — правка кода, а не ALTER.
    """

    REQUEST_CREATED = "request_created"
    REQUEST_CLAIMED = "request_claimed"
    REQUEST_DONE = "request_done"
    REQUEST_CANCELLED = "request_cancelled"
    # Передача заявки (spec 0042): вернуть в очередь и сменить службу.
    REQUEST_RETURNED = "request_returned"
    REQUEST_TRANSFERRED = "request_transferred"
    # Отметка «Оплачено» у заказа в номер (spec 0038 Р-2, spec 0041 §2.2).
    REQUEST_PAYMENT_MARKED = "request_payment_marked"
    STAY_CHECKED_IN = "stay_checked_in"
    STAY_CHECKED_OUT = "stay_checked_out"
    STAY_MOVED = "stay_moved"
    STAY_EXTENDED = "stay_extended"
    STAY_CODE_REISSUED = "stay_code_reissued"
    # Автовыезд по сроку (spec 0043 §2.3) — действие без личности: пишет воркер,
    # в журнале — «автоматически».
    STAY_AUTO_CHECKED_OUT = "stay_auto_checked_out"
    # Сверка со снимком Opera (spec 0058 §2.5) — тоже без личности, тем же воркером.
    STAY_PMS_CHECKED_IN = "stay_pms_checked_in"
    STAY_PMS_LINKED = "stay_pms_linked"
    STAY_PMS_MOVED = "stay_pms_moved"
    STAY_PMS_EXTENDED = "stay_pms_extended"
    STAY_PMS_CHECKED_OUT = "stay_pms_checked_out"
    # Реквизиты для АВР (spec 0049 Р-10): правка поля и «Проверено» — над
    # запросом, «Что» — его заявка; значений полей в журнале нет.
    ACT_REQUISITES_EDITED = "act_requisites_edited"
    ACT_REQUISITES_CHECKED = "act_requisites_checked"


_staff_action_column_type = Enum(
    StaffAction,
    name="staff_action",
    native_enum=False,
    length=32,
    values_callable=lambda members: [member.value for member in members],
)


class StaffActionLog(Base):
    """Строка журнала изменений: кто, когда, что и над чем (spec 0041 §2.1).

    Пишется той же сессией, что само действие, — откат действия откатывает и
    строку. Объект действия — снапшоты (`#N`, служба, комната), а не join:
    журнал свидетельствует «как было в момент действия». `request_id`/`stay_id`
    без FK — ядро не ссылается на таблицы модулей. Текстов гостя и имён здесь
    нет по построению: `with_note` говорит только, что примечание было.
    Срок хранения — как у текстов гостей (spec 0032), удаляет джоба ретеншна.
    """

    __tablename__ = "staff_action_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    # Пара «FK + снапшот», как у `claimed_by_*` заявки; логин — PII
    # (docs/PII_REGISTRY.md), NULL — учётка без логина отеля.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    actor_login: Mapped[str | None] = mapped_column(String(12))
    action: Mapped[StaffAction] = mapped_column(_staff_action_column_type)
    request_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    stay_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    # Номер `#N` уникален только внутри дня — без дня его не отличить.
    request_daily_number: Mapped[int | None] = mapped_column(Integer())
    request_service_day: Mapped[date | None] = mapped_column(Date())
    category_name: Mapped[str | None] = mapped_column(String(255))
    # У передачи заявки `category_name` — откуда, `category_name_to` — куда
    # (spec 0042 §2.3, миграция 0031) — по образцу пары комнат переселения.
    category_name_to: Mapped[str | None] = mapped_column(String(255))
    # Служба строки — id категории (без FK, как `request_id`), та же пара
    # «откуда / куда»: по ней старший видит строки своих служб (spec 0045 §3,
    # миграция 0034). Подпись для этого не годится — она не уникальна и
    # меняется. Строки до 0034 — NULL: их видят только менеджер и ресепшен.
    category_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    category_id_to: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    # У переселения `room_number` — откуда, `room_number_to` — куда.
    room_number: Mapped[str | None] = mapped_column(String(20))
    room_number_to: Mapped[str | None] = mapped_column(String(20))
    nights: Mapped[int | None] = mapped_column(SmallInteger())
    with_note: Mapped[bool] = mapped_column(default=False, server_default=false())


# Единственное чтение журнала — день тенанта, новые сверху (страница
# `/staff/{slug}/changes`), и удаление ретеншна по `occurred_at`.
Index(
    "ix_staff_action_log_tenant_id_occurred_at",
    StaffActionLog.tenant_id,
    StaffActionLog.occurred_at.desc(),
)
