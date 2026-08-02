"""Конфигурация тенанта (Task 0011, FOUNDATION §6, ADR-003).

CANONICAL: канон конфигурации тенанта — различия отелей живут здесь как
данные, а не как ветки кода (P-11).

- Хранение — JSONB-колонка `tenants.config`; `NULL` = тенант создан, но
  онбординг не завершён. Форму задаёт Pydantic-схема `TenantConfig` (P-7),
  в корне — `schema_version` (§6).
- Чтение и запись — только через `load_tenant_config` / `store_tenant_config`
  (P-12): это единственный путь, на котором конфиг гарантированно проходит
  схему. Прямая работа с колонкой разрешена только внутри модуля `platform`.
- Эволюция схемы (§6): новое НЕобязательное поле со значением по умолчанию
  не повышает `schema_version`; несовместимое изменение — повышение версии +
  скрипт миграции конфигов всех тенантов с той же дисциплиной, что Alembic
  для БД. Первый такой скрипт появится вместе с первым несовместимым
  изменением.

Не путать с `shared/config.py`: там — настройки ОКРУЖЕНИЯ процесса
(переменные окружения, одни на инсталляцию), здесь — настройки ТЕНАНТА
(строка в БД, у каждого отеля свои).

Отображаемое имя отеля живёт в `tenants.name` (единственный источник),
в конфиг оно не дублируется.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.platform.models import Tenant
from hospitality.shared.errors import AppError

# Версия структуры конфига (§6). Повышается только при несовместимом
# изменении схемы — вместе со скриптом миграции конфигов всех тенантов.
TENANT_CONFIG_SCHEMA_VERSION: Final = 1

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
TENANT_NOT_FOUND_ERROR_CODE = "ERR-PLATFORM-004"
TENANT_NOT_CONFIGURED_ERROR_CODE = "ERR-PLATFORM-005"
TENANT_CONFIG_INVALID_ERROR_CODE = "ERR-PLATFORM-006"

# Формат ключа категории заявок — копия паттерна `RequestCategoryCreate.key`
# (модуль requests). Дублируется намеренно: kernel не импортирует доменные
# модули (R-5), а опечатка в ключе обязана падать здесь, а не молча выключать
# маршрутизацию уведомлений (spec 0026).
_CATEGORY_KEY_PATTERN: Final = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Границы срока напоминания о невзятой заявке (spec 0028): минута — нижний
# осмысленный предел (0 означал бы «мгновенно», то есть шум вместо сигнала),
# неделя — верхний (всё, что больше, — опечатка, а не настройка отеля).
_MIN_REMINDER_MINUTES: Final = 1
_MAX_REMINDER_MINUTES: Final = 7 * 24 * 60

# Платформенный срок напоминания по умолчанию (spec 0028): отель, который
# ничего не настроил, обязан получать защиту — сегодняшняя тишина и есть
# дефект, ради которого заведена issue #57. Выключение — явный `null`.
DEFAULT_REQUEST_REMINDER_MINUTES: Final = 30

# Предел длины подсказки категории (issue #123): подсказка уходит в описание
# инструмента КАЖДЫМ ходом диалога, то есть оплачивается токенами постоянно.
# Это короткий список типовых предметов, а не должностная инструкция службы.
MAX_CATEGORY_HINT_LENGTH: Final = 300


class HotelProfile(BaseModel):
    """Профиль отеля — описательная часть конфигурации (§6).

    Phase 0 — минимум для демо-тенанта; адрес, контакты и прочее добавляются
    необязательными полями по мере надобности (см. «эволюция схемы» выше).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    city: str = Field(min_length=1, max_length=100)
    # ISO 3166-1 alpha-2: "KZ", а не "Казахстан" — коды не требуют перевода.
    country_code: str = Field(pattern=r"^[A-Z]{2}$")


class TenantConfig(BaseModel):
    """Схема конфигурации тенанта (§6): schema_version, профиль, пояс, язык.

    `extra="forbid"`: опечатка в имени поля — ошибка валидации, а не молча
    проигнорированная настройка. `frozen=True`: конфиг — значение; менять —
    через `store_tenant_config` целиком.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = TENANT_CONFIG_SCHEMA_VERSION
    profile: HotelProfile
    # IANA-имя ("Asia/Almaty"): канон времени §9 — в БД UTC, локальное время
    # отеля вычисляется из этого пояса (свойство `tzinfo`).
    timezone: str
    # ISO 639-1 ("ru", "kk", "en"): язык ответов гостю по умолчанию.
    default_language: str = Field(pattern=r"^[a-z]{2}$")
    # Маршрутизация уведомлений по службам (spec 0026, issue #80):
    # `key` категории заявок → внешний id staff-чата канала уведомлений
    # (Phase 0 — Telegram `chat.id` строкой, как `Conversation.external_id`).
    # Пусто = всё в дефолтный чат инсталляции (`TELEGRAM_STAFF_CHAT_ID`) —
    # поведение до этой спеки. Поле аддитивное → `schema_version` остаётся 1 (§6).
    # `frozen=True` защищает поля модели, а не содержимое словаря; подмены
    # между чтениями это не даёт: `load_tenant_config` каждый раз валидирует
    # конфиг из JSONB заново, то есть отдаёт новый словарь.
    staff_chats_by_category: dict[str, str] = Field(default_factory=dict)
    # Телефон ресепшена для статического auth-only ответа веб-чата (spec 0027
    # §3.1, ADR-008 Q7): показывается неавторизованному/выехавшему гостю.
    # Необязательное поле со значением по умолчанию → schema_version остаётся 1
    # (§6). Формат свободный (показывается как есть): «+7 727 …», добавочный и
    # т.п. — kernel не валидирует телефонные форматы мира.
    reception_phone: str | None = Field(default=None, max_length=32)
    # Срок, после которого невзятая заявка подсвечивается напоминанием в чат
    # службы (spec 0028, issue #57). Минуты, а не часы: одним полем выражаются
    # и «15 минут» для прорыва трубы, и «4 часа» для смены белья. `None` —
    # напоминания у этого отеля выключены. Пер-категорийный словарь ниже
    # переопределяет базовый срок для своих категорий (уборка ≠ прорыв трубы).
    # Оба поля необязательные → `schema_version` остаётся 1 (§6).
    request_reminder_after_minutes: int | None = Field(
        default=DEFAULT_REQUEST_REMINDER_MINUTES,
        ge=_MIN_REMINDER_MINUTES,
        le=_MAX_REMINDER_MINUTES,
    )
    request_reminder_minutes_by_category: dict[str, int] = Field(default_factory=dict)
    # Типовые предметы службы: `key` категории → короткий список («кофе в
    # пакетиках, чайник, вода, полотенца»). Уходит в описание enum'а
    # `create_service_request` (issue #123, живой случай 31.07: «2 пачки кофе»).
    # Зачем данные тенанта, а не колонка `request_categories`: адрес доставки
    # определяет не предмет, а его вид — кофе в пакетиках выдаёт housekeeping,
    # сваренный F&B, — и этот раздел у каждого отеля свой. Здесь же лежат
    # остальные пер-категорийные настройки тенанта (чаты, сроки) — одно место
    # правки (P-12), без миграции и без доменного смысла: подсказку читает
    # только AI-слой, домен о ней не знает. Один и тот же предмет НАМЕРЕННО
    # называется у двух служб: так модель видит неоднозначность сама и по
    # канону промпта спрашивает гостя, вместо того чтобы угадывать.
    # Поле аддитивное → `schema_version` остаётся 1 (§6).
    category_hints: dict[str, str] = Field(default_factory=dict)

    @field_validator("category_hints")
    @classmethod
    def _category_hints_must_be_well_formed(cls, value: dict[str, str]) -> dict[str, str]:
        for category_key, hint in value.items():
            if not _CATEGORY_KEY_PATTERN.match(category_key):
                raise ValueError(f"not a category key: {category_key!r}")
            # Пустая подсказка — это «подсказки у службы нет», и выражается она
            # отсутствием ключа: иначе в описании инструмента появится пустая
            # строка, которую модель прочтёт как «сюда ничего не относится».
            if not hint.strip():
                raise ValueError(f"empty hint for category {category_key!r}")
            if len(hint) > MAX_CATEGORY_HINT_LENGTH:
                raise ValueError(
                    f"hint for category {category_key!r} is longer than "
                    f"{MAX_CATEGORY_HINT_LENGTH} characters: {len(hint)}"
                )
        return value

    @field_validator("request_reminder_minutes_by_category")
    @classmethod
    def _reminder_minutes_must_be_well_formed(cls, value: dict[str, int]) -> dict[str, int]:
        for category_key, minutes in value.items():
            if not _CATEGORY_KEY_PATTERN.match(category_key):
                raise ValueError(f"not a category key: {category_key!r}")
            # Те же границы, что у базового поля: правило одно, мест два —
            # словарь Pydantic сам не валидирует по границам ключа-значения.
            if not _MIN_REMINDER_MINUTES <= minutes <= _MAX_REMINDER_MINUTES:
                raise ValueError(
                    f"reminder minutes for category {category_key!r} must be "
                    f"between {_MIN_REMINDER_MINUTES} and {_MAX_REMINDER_MINUTES}: {minutes}"
                )
        return value

    @field_validator("staff_chats_by_category")
    @classmethod
    def _staff_chats_must_be_well_formed(cls, value: dict[str, str]) -> dict[str, str]:
        for category_key, chat_id in value.items():
            if not _CATEGORY_KEY_PATTERN.match(category_key):
                raise ValueError(f"not a category key: {category_key!r}")
            # Пустой адрес — это «уведомления службы выключены» молча; такую
            # настройку выражают удалением ключа, а не пустой строкой.
            if not chat_id.strip():
                raise ValueError(f"empty staff chat id for category {category_key!r}")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_must_be_iana(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"not an IANA timezone name: {value!r}") from exc
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        """Часовой пояс отеля для слоя представления (§9: в БД — только UTC)."""
        return ZoneInfo(self.timezone)

    def staff_chat_for(self, category_key: str | None, *, default: str) -> str:
        """Чат службы для категории; нет маппинга (или категории) — `default`.

        Единственное место правила фолбэка (P-12, spec 0026): им пользуются и
        подписчики-уведомления, и всё, что появится за ними (SLA Phase 1).
        """
        if category_key is None:
            return default
        return self.staff_chats_by_category.get(category_key, default)

    def staff_chat_ids(self, *, default: str) -> frozenset[str]:
        """Все чаты персонала тенанта: дефолтный + чаты служб (spec 0026).

        Граница «кто персонал» (`channels/telegram/service.py`): входящее из
        этих чатов — команды сотрудника, любое другое — реплика гостя. Поэтому
        множество строк со СТРОГИМ равенством, а не сравнение с одной строкой:
        подстрочные совпадения id тут недопустимы. Пустые значения отсеиваются —
        ненастроенный дефолт не делает персоналом чат с пустым id.
        """
        chats = {default, *self.staff_chats_by_category.values()}
        return frozenset(chat_id for chat_id in chats if chat_id)

    def reminder_delay_for(self, category_key: str | None) -> timedelta | None:
        """Срок ожидания до напоминания для категории; None — напоминаний нет.

        Единственное место правила (P-12, spec 0028), как `staff_chat_for`:
        пер-категорийный срок переопределяет базовый, его отсутствие — базовый.
        Заявка с незнакомой категорией (`None`) получает базовый срок: она
        реальна, даже если категория не резолвится.
        """
        if category_key is not None:
            minutes = self.request_reminder_minutes_by_category.get(category_key)
            if minutes is not None:
                return timedelta(minutes=minutes)
        if self.request_reminder_after_minutes is None:
            return None
        return timedelta(minutes=self.request_reminder_after_minutes)

    def min_reminder_delay(self) -> timedelta | None:
        """Самый ранний срок напоминания тенанта; None — напоминания выключены.

        Граница выборки кандидатов одним запросом (spec 0028 §4): заявки моложе
        неё не просрочены ни по одному сроку этого отеля. Точный срок каждой —
        уже `reminder_delay_for` по её категории.
        """
        candidates = list(self.request_reminder_minutes_by_category.values())
        if self.request_reminder_after_minutes is not None:
            candidates.append(self.request_reminder_after_minutes)
        return timedelta(minutes=min(candidates)) if candidates else None


async def load_tenant_config(session: AsyncSession, tenant_id: uuid.UUID) -> TenantConfig:
    """Прочитать конфигурацию тенанта (канонический путь чтения, P-12).

    Ожидаемые ошибки — `AppError` с кодами каталога: тенант не найден (404),
    конфиг не задан — онбординг не завершён (409), конфиг в БД не проходит
    схему — дрейф данных, см. статью ERR-PLATFORM-006 (500).
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError(
            code=TENANT_NOT_FOUND_ERROR_CODE,
            message="Тенант не найден",
            status_code=404,
        )
    if tenant.config is None:
        raise AppError(
            code=TENANT_NOT_CONFIGURED_ERROR_CODE,
            message="Конфигурация тенанта не задана: онбординг не завершён",
            status_code=409,
        )
    try:
        return TenantConfig.model_validate(tenant.config)
    except ValidationError as exc:
        raise AppError(
            code=TENANT_CONFIG_INVALID_ERROR_CODE,
            message="Конфигурация тенанта не соответствует схеме",
            status_code=500,
        ) from exc


async def load_tenant_name(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """Отображаемое имя отеля (`tenants.name` — единственный источник, см. шапку).

    Отдельная функция, а не чтение колонки на местах: имя нужно поверхностям,
    которые говорят с гостем от лица отеля (приветствие консьержа, issue #39),
    и «где живёт имя» обязано решаться один раз (P-12). `session` —
    платформенная (`platform_session_scope`): `tenants` — реестр, а не
    тенантная таблица.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError(
            code=TENANT_NOT_FOUND_ERROR_CODE,
            message="Тенант не найден",
            status_code=404,
        )
    return tenant.name


async def list_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    """ВСЕ тенанты реестра — обход задачами, не зависящими от онбординга.

    Отличие от `list_configured_tenant_ids`: конфиг не требуется. Ретеншн
    гостевых текстов (issue #42, spec 0032 §3) обязан пройти и по тенанту без
    завершённого онбординга — данные (и юр-обязанность их удалить) у него есть,
    а настройки задачи он не даёт: срок — свойство инсталляции.

    `session` — платформенная (`platform_session_scope`): `tenants` — реестр,
    а не тенантная таблица.
    """
    rows = await session.scalars(select(Tenant.id).order_by(Tenant.created_at, Tenant.id))
    return list(rows)


async def list_configured_tenant_ids(session: AsyncSession) -> list[uuid.UUID]:
    """Тенанты с завершённым онбордингом (конфиг задан) — обход фоновыми задачами.

    Фоновая задача не имеет входящего запроса и обязана сама обойти тенантов:
    берёт этот список платформенной сессией, а дальше работает под
    `tenant_context` каждого (P-4 — кросс-тенантных запросов к бизнес-таблицам
    не появляется). Тенант без конфига пропускается: у него не из чего взять
    ни срок, ни адресат (§6: `NULL` = онбординг не завершён).

    `session` — платформенная (`platform_session_scope`): `tenants` — реестр,
    а не тенантная таблица.
    """
    rows = await session.scalars(
        select(Tenant.id).where(Tenant.config.is_not(None)).order_by(Tenant.created_at, Tenant.id)
    )
    return list(rows)


async def store_tenant_config(
    session: AsyncSession, tenant_id: uuid.UUID, config: TenantConfig
) -> None:
    """Записать конфигурацию тенанта целиком (канонический путь записи, P-12).

    Тип аргумента гарантирует валидность: в колонку попадает только
    `model_dump` уже прошедшей схему модели.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError(
            code=TENANT_NOT_FOUND_ERROR_CODE,
            message="Тенант не найден",
            status_code=404,
        )
    tenant.config = config.model_dump(mode="json")
