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

import uuid
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
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
