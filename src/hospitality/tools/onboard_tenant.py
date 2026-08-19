"""Онбординг отеля: тенант + категории служб + конфиг из файла профиля (#123).

Канонический путь «подключить настоящий отель» (§6 «Онбординг отеля»). Отличие
от `tools/seed.py`: сид создаёт ДЕМО-данные dev/CI одного фиксированного состава
(три категории Demo Hotel) и бежит на каждом деплое; онбординг — осознанное
разовое действие оператора над конкретным отелем, состав служб берётся из файла
профиля, а не из кода (P-11: различия отелей — данные).

Что делает (идемпотентно, повторный запуск безопасен):

1. тенанта со `--slug` создаёт, если его нет (нужен `--name`), иначе берёт;
2. категории из профиля — создаёт недостающие, существующие не трогает
   (переименование службы — отдельное осознанное действие, не онбординг);
3. конфиг тенанта пересобирает из профиля и пишет каноническим путём
   (`store_tenant_config`): пояс, язык, телефон ресепшена, сроки напоминаний
   (spec 0028) и подсказки служб (`category_hints`).

`staff_chats_by_category` НЕ трогается: чаты служб — отдельная операция
(`tools/staff_routing`), их id зависят от инсталляции, а не от отеля.

Запуск (локально; на staging — то же внутри контейнера, префиксом
`docker compose -f /opt/hospitality/docker-compose.staging.yml exec app`):

    python -m hospitality.tools.onboard_tenant ops/onboarding/pilot-hotel.json \\
        --slug pilot-hotel --name "Название отеля" --reception-phone "+7 727 000 00 00"

    python -m hospitality.tools.onboard_tenant ops/onboarding/pilot-hotel.json \\
        --slug demo-hotel            # репетиция профиля пилота на demo (staging)

Чек-лист онбординга целиком — docs/runbooks/tenant-onboarding.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    RequestCategoryCreate,
    create_category,
    list_categories,
)
from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    HotelProfile,
    TenantConfig,
    load_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)


class OnboardingError(Exception):
    """Ошибка ввода оператора: понятный текст вместо трассировки."""


class ProfileCategory(BaseModel):
    """Служба отеля в файле профиля: адрес доставки заявки + её настройки.

    Всё, что относится к одной службе, лежит в одной записи — оператор правит
    её целиком, а не ищет ключ в трёх параллельных словарях конфига.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    name: str
    # Свой срок напоминания (spec 0028); не задан — действует базовый.
    reminder_after_minutes: int | None = None
    # Типовые предметы службы для описания инструмента (#123). Предмет,
    # который выдают две службы, называется у ОБЕИХ: так модель видит
    # неоднозначность и спрашивает гостя вместо угадывания.
    hints: str | None = None


class TenantProfile(BaseModel):
    """Профиль отеля из JSON-файла: всё, что не зависит от инсталляции.

    Идентичность отеля (slug, отображаемое имя, телефон ресепшена) в файл НЕ
    входит — она приходит аргументами команды: репозиторий публичный, а имя и
    телефон живого отеля в нём не место.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    city: str
    country_code: str
    timezone: str
    default_language: str
    # Базовый срок напоминания: действует для заявок, чья категория не
    # резолвится. `null` — напоминания у отеля выключены (spec 0028). Поле
    # обязательное и без значения по умолчанию: забытая строка в профиле не
    # должна означать «напоминания выключены» — это решение, а не умолчание.
    request_reminder_after_minutes: int | None
    categories: tuple[ProfileCategory, ...] = Field(min_length=1)


class OnboardingResult(BaseModel):
    """Что получилось — для отчёта оператору и для тестов."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    tenant_created: bool
    categories_created: tuple[str, ...]
    categories_existing: tuple[str, ...]
    # Категории тенанта вне профиля: онбординг их не удаляет, но они остаются
    # живым адресом доставки и продолжают попадать в enum инструмента.
    categories_extra: tuple[str, ...]
    config: TenantConfig


def load_profile(path: Path) -> TenantProfile:
    """Прочитать и проверить файл профиля (ошибки — текстом, а не трассировкой).

    Проверка двойная: схема самого профиля и конфиг, который из него получится
    (длина подсказок, формат ключей, часовой пояс — их знает `TenantConfig`).
    Второе — ДО обращения к БД: профиль с опечаткой не должен успеть создать
    тенанта и категории и упасть на записи конфига.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise OnboardingError(f"Не читается файл профиля {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise OnboardingError(f"{path} — не валидный JSON: {error}") from error
    try:
        profile = TenantProfile.model_validate(raw)
    except ValidationError as error:
        raise OnboardingError(f"{path} не соответствует схеме профиля:\n{error}") from error
    build_config(profile, reception_phone=None, previous=None)
    return profile


def build_config(
    profile: TenantProfile,
    *,
    reception_phone: str | None,
    previous: TenantConfig | None,
) -> TenantConfig:
    """Собрать конфиг тенанта из профиля (P-12: запись — через `store_tenant_config`).

    Профиль задаёт конфиг ЦЕЛИКОМ, кроме двух вещей: `staff_chats_by_category`
    переносится из прежнего конфига (это отдельная операция `staff_routing`), а
    телефон ресепшена берётся из аргумента или, если его не передали, из
    прежнего конфига — чтобы повторный онбординг не стёр уже настроенный номер.

    Схема конфига — последний рубеж проверки данных отеля (длина подсказки,
    формат ключа, пояс): её отказ превращается в текст оператору, а не в
    трассировку — команду запускает основатель, а не разработчик.
    """
    try:
        return TenantConfig(
            profile=HotelProfile(city=profile.city, country_code=profile.country_code),
            timezone=profile.timezone,
            default_language=profile.default_language,
            staff_chats_by_category=dict(previous.staff_chats_by_category) if previous else {},
            reception_phone=reception_phone or (previous.reception_phone if previous else None),
            request_reminder_after_minutes=profile.request_reminder_after_minutes,
            request_reminder_minutes_by_category={
                category.key: category.reminder_after_minutes
                for category in profile.categories
                if category.reminder_after_minutes is not None
            },
            category_hints={
                category.key: category.hints for category in profile.categories if category.hints
            },
        )
    except ValidationError as error:
        raise OnboardingError(
            f"Профиль и аргументы не дают валидный конфиг тенанта:\n{error}"
        ) from error


async def _ensure_tenant(slug: str, name: str | None) -> tuple[uuid.UUID, bool]:
    """Тенант со `slug`: вернуть его id и признак «создан этим запуском».

    Тенанта нет и имя не передано — отказ: опечатка в `--slug` не должна
    молча плодить пустых тенантов (тот же принцип, что у `staff_routing`).
    """
    async with platform_session_scope() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is not None:
            return tenant.id, False
        if name is None:
            raise OnboardingError(
                f"Тенанта со slug {slug!r} нет. Чтобы создать нового — передайте --name "
                f'"Название отеля"; если отель уже заведён, проверьте slug на опечатку.'
            )
        tenant = Tenant(slug=slug, name=name)
        session.add(tenant)
        await session.flush()  # id нужен для лога и возврата
        logger.info("tenant_created", tenant_id=str(tenant.id), slug=slug)
        return tenant.id, True


async def _ensure_categories(
    tenant_id: uuid.UUID, categories: tuple[ProfileCategory, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Создать недостающие категории; вернуть (созданные, уже бывшие, лишние).

    Существующую категорию не трогаем даже при другом `name`: переименование
    службы — отдельное осознанное действие, а не побочный эффект онбординга.
    «Лишние» — категории тенанта вне профиля: онбординг их не удаляет (за ними
    могут стоять живые заявки), но обязан о них сказать.
    """
    created: list[str] = []
    existing: list[str] = []
    with tenant_context(tenant_id):
        for category in categories:
            try:
                await create_category(RequestCategoryCreate(key=category.key, name=category.name))
            except AppError as error:
                if error.code != ERR_REQUESTS_CATEGORY_KEY_TAKEN:
                    raise
                existing.append(category.key)
                continue
            created.append(category.key)
        profile_keys = {category.key for category in categories}
        extra = sorted(item.key for item in await list_categories() if item.key not in profile_keys)
    if extra:
        logger.info("onboarding_extra_categories", tenant_id=str(tenant_id), categories=extra)
    return tuple(created), tuple(existing), tuple(extra)


async def onboard_tenant(
    *,
    slug: str,
    name: str | None,
    profile: TenantProfile,
    reception_phone: str | None,
) -> OnboardingResult:
    """Онбординг отеля целиком: тенант → категории → конфиг (идемпотентно)."""
    tenant_id, tenant_created = await _ensure_tenant(slug, name)
    created, existing, extra = await _ensure_categories(tenant_id, profile.categories)
    async with platform_session_scope() as session:
        previous: TenantConfig | None = None
        try:
            previous = await load_tenant_config(session, tenant_id)
        except AppError as error:
            # Конфига нет — онбординг как раз и не был завершён (§6). Любая
            # другая ошибка (дрейф схемы, ERR-PLATFORM-006) — наружу: молча
            # затирать конфиг, который мы не смогли прочитать, нельзя.
            if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
                raise
        config = build_config(profile, reception_phone=reception_phone, previous=previous)
        await store_tenant_config(session, tenant_id, config)
    logger.info(
        "tenant_onboarded",
        tenant_id=str(tenant_id),
        slug=slug,
        categories_created=list(created),
        categories_total=len(profile.categories),
    )
    return OnboardingResult(
        tenant_id=tenant_id,
        tenant_created=tenant_created,
        categories_created=created,
        categories_existing=existing,
        categories_extra=extra,
        config=config,
    )


def format_report(slug: str, result: OnboardingResult) -> str:
    """Отчёт оператору: что стало с тенантом, категориями и конфигом."""
    config = result.config
    lines = [
        f"Тенант {slug} ({'создан' if result.tenant_created else 'уже существовал'}): "
        f"{result.tenant_id}",
        f"Категории: создано {len(result.categories_created)} "
        f"({', '.join(result.categories_created) or '—'}); "
        f"уже были {len(result.categories_existing)} "
        f"({', '.join(result.categories_existing) or '—'})",
        f"Конфиг: {config.profile.city}/{config.profile.country_code}, "
        f"пояс {config.timezone}, язык {config.default_language}",
    ]
    if result.categories_extra:
        lines.append(
            f"Категории вне профиля (оставлены как есть, гость их видит): "
            f"{', '.join(result.categories_extra)}"
        )
    if config.reception_phone:
        lines.append(f"Телефон ресепшена: {config.reception_phone}")
    else:
        lines.append(
            "Телефон ресепшена НЕ задан: неавторизованный гость в веб-чате "
            "получит отказ без телефона, а гость, сообщивший о ЧП, — текст "
            "перехвата без строки ресепшена (останется только 112); "
            "задаётся --reception-phone"
        )
    base = config.request_reminder_after_minutes
    lines.append(f"Напоминания, базовый срок: {base} мин" if base else "Напоминания выключены")
    for key, minutes in sorted(config.request_reminder_minutes_by_category.items()):
        lines.append(f"  {key}: {minutes} мин")
    lines.append(f"Подсказки служб: {len(config.category_hints)}")
    for key, hint in sorted(config.category_hints.items()):
        lines.append(f"  {key}: {hint}")
    chats = len(config.staff_chats_by_category)
    lines.append(
        f"Чаты служб: {chats} (перенесены как были; задаются `python -m "
        f"hospitality.tools.staff_routing`)"
    )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.onboard_tenant",
        description="Онбординг отеля: тенант, категории служб, конфиг из файла профиля (#123).",
    )
    parser.add_argument("profile", type=Path, help="Файл профиля отеля (JSON).")
    parser.add_argument(
        "--slug",
        default=get_settings().telegram_tenant_slug,
        help="Slug тенанта (по умолчанию — TELEGRAM_TENANT_SLUG).",
    )
    parser.add_argument(
        "--name",
        help="Отображаемое имя отеля. Обязательно, если тенанта ещё нет; иначе не используется.",
    )
    parser.add_argument(
        "--reception-phone",
        help="Телефон ресепшена для статического ответа неавторизованному гостю (spec 0027).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI; возвращает код возврата процесса."""
    configure_logging(get_settings().log_level)
    args = _build_parser().parse_args(argv)
    try:
        profile = load_profile(args.profile)
        result = asyncio.run(
            onboard_tenant(
                slug=args.slug,
                name=args.name,
                profile=profile,
                reception_phone=args.reception_phone,
            )
        )
    except OnboardingError as error:
        print(str(error), file=sys.stderr)
        return 1
    except AppError as error:
        # Ожидаемые отказы ядра (конфиг не проходит схему — ERR-PLATFORM-006):
        # оператору текст и код каталога, не трассировка.
        print(f"{error.message} ({error.code}, docs/runbooks/errors.md)", file=sys.stderr)
        return 1
    print(format_report(args.slug, result))
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
