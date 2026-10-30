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
   (spec 0028), подсказки служб (`category_hints`), службу, в которой
   эскалация заводит заявку (`--escalation-category`, spec 0039 §2.3), и
   room service (`room_service`, spec 0038 Р-10).

Чаты НЕ трогаются: и `staff_chats_by_category` (`tools/staff_routing`), и пара
настроек утренней сводки `daily_summary_chat_id` / `daily_summary_local_time`
(`tools/daily_summary`, spec 0035 §8) переносятся из прежнего конфига — их id
зависят от инсталляции, а не от отеля, и узнают их, добавив бота в группу.

Запуск (локально; на staging — то же внутри контейнера, префиксом
`docker compose -f /opt/hospitality/docker-compose.staging.yml exec app`):

    python -m hospitality.tools.onboard_tenant ops/onboarding/pilot-hotel.json \\
        --slug <код-отеля> --name "Название отеля" --reception-phone "+7 727 000 00 00"

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
    DEFAULT_DAILY_SUMMARY_LOCAL_TIME,
    DEFAULT_STAY_AUTO_CHECKOUT_GRACE_HOURS,
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    HotelProfile,
    RoomServiceConfig,
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
    # Запас автовыезда после расчётного часа, часы (spec 0043 §2.1); `null` —
    # автовыезд выключен (отель с выгрузкой из PMS). В отличие от напоминаний,
    # у поля есть умолчание: профили до #225 его не знают, а забытая строка
    # даёт платформенную защиту, а не её отсутствие.
    stay_auto_checkout_grace_hours: int | None = DEFAULT_STAY_AUTO_CHECKOUT_GRACE_HOURS
    categories: tuple[ProfileCategory, ...] = Field(min_length=1)
    # Room service (spec 0038 Р-10): флаг, языки, у каталогов служба, сбор и
    # заметка о доставке. Нет ключа — профиль room service не задаёт, и прежнее
    # значение переносится (`build_config`): публичный профиль в репозитории
    # не несёт сбора пилота, и его повторный прогон не должен стирать
    # настроенное из файла `*.local`. Выключить — `"enabled": false`.
    room_service: RoomServiceConfig | None = None


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
    reception_room_dial: str | None = None,
    escalation_category_key: str | None = None,
) -> TenantConfig:
    """Собрать конфиг тенанта из профиля (P-12: запись — через `store_tenant_config`).

    Профиль задаёт конфиг ЦЕЛИКОМ, кроме перечисленного ниже:
    `staff_chats_by_category`, пара настроек утренней сводки (`daily_summary_chat_id` /
    `daily_summary_local_time`), справочник отеля `hotel_facts`, флаг раздела
    «Наставник» `mentor_enabled` (spec 0048 §2) и сроки «долго в работе»
    `request_stalled_*` (spec 0028 §8) переносятся из прежнего конфига — это
    отдельные операции (`staff_routing`, `daily_summary`, `request_reminders --stalled`,
    страница «Справочник отеля», включение раздела руками), потому что id чата
    узнают, добавив бота в группу, а факты пишет менеджер отеля, — ни то, ни
    другое не выписывают в файл профиля; телефон ресепшена, что набрать на
    ресепшен из номера и служба эскалаций берутся из аргументов или, если их не
    передали, из прежнего конфига — чтобы повторный онбординг не стёр уже
    настроенное (служба эскалаций, стёртая молча, вернула бы отелю без бота
    эскалации в никуда — spec 0039 §2.3).

    Перенос здесь не косметика: без него повторный онбординг (правка профиля
    отеля) молча выключал бы утреннюю сводку — чат обнулялся бы в `None`, а
    менеджер увидел бы это только не получив сообщение — и так же молча стирал
    бы весь справочник отеля: до 30 фактов, набранных менеджером руками, а
    узнали бы об этом по тому, что бот перестал знать про Wi-Fi (spec 0036 §10
    прямо запрещает заполнять справочник этой командой — источник у поля всегда
    внешний, значит перенос обязателен).

    Room service (spec 0038 Р-10) берётся из профиля, а если профиль его не
    задаёт — из прежнего конфига: публичный профиль в репозитории сбора пилота
    не несёт, и его прогон не должен выключать room service, настроенный из
    файла `*.local`. Служба каталога из профиля обязана быть категорией того же
    профиля — иначе заказ ушёл бы в службу, которой у отеля нет.

    Схема конфига — последний рубеж проверки данных отеля (длина подсказки,
    формат ключа, пояс): её отказ превращается в текст оператору, а не в
    трассировку — команду запускает основатель, а не разработчик.
    """
    _check_room_service_categories(profile)
    try:
        return TenantConfig(
            profile=HotelProfile(city=profile.city, country_code=profile.country_code),
            timezone=profile.timezone,
            default_language=profile.default_language,
            staff_chats_by_category=dict(previous.staff_chats_by_category) if previous else {},
            daily_summary_chat_id=previous.daily_summary_chat_id if previous else None,
            daily_summary_local_time=(
                previous.daily_summary_local_time if previous else DEFAULT_DAILY_SUMMARY_LOCAL_TIME
            ),
            hotel_facts=list(previous.hotel_facts) if previous else [],
            mentor_enabled=previous.mentor_enabled if previous else False,
            reception_phone=reception_phone or (previous.reception_phone if previous else None),
            reception_room_dial=(
                reception_room_dial or (previous.reception_room_dial if previous else None)
            ),
            escalation_category_key=(
                escalation_category_key or (previous.escalation_category_key if previous else None)
            ),
            request_reminder_after_minutes=profile.request_reminder_after_minutes,
            stay_auto_checkout_grace_hours=profile.stay_auto_checkout_grace_hours,
            request_reminder_minutes_by_category={
                category.key: category.reminder_after_minutes
                for category in profile.categories
                if category.reminder_after_minutes is not None
            },
            request_stalled_after_minutes=(
                previous.request_stalled_after_minutes if previous else None
            ),
            request_stalled_minutes_by_category=(
                dict(previous.request_stalled_minutes_by_category) if previous else {}
            ),
            category_hints={
                category.key: category.hints for category in profile.categories if category.hints
            },
            room_service=(
                profile.room_service
                if profile.room_service is not None
                else (previous.room_service if previous else None)
            ),
        )
    except ValidationError as error:
        raise OnboardingError(
            f"Профиль и аргументы не дают валидный конфиг тенанта:\n{error}"
        ) from error


def _check_room_service_categories(profile: TenantProfile) -> None:
    """Служба каждого каталога room service — категория из профиля (spec 0038 §6)."""
    if profile.room_service is None:
        return
    known = {category.key for category in profile.categories}
    for catalog, settings in profile.room_service.catalogs.items():
        if settings.category_key not in known:
            raise OnboardingError(
                f"room_service: у каталога {catalog!r} служба {settings.category_key!r}, "
                f"а в categories профиля такой нет. Добавьте службу в categories или "
                f"исправьте ключ."
            )


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
    reception_room_dial: str | None = None,
    escalation_category_key: str | None = None,
) -> OnboardingResult:
    """Онбординг отеля целиком: тенант → категории → конфиг (идемпотентно)."""
    tenant_id, tenant_created = await _ensure_tenant(slug, name)
    created, existing, extra = await _ensure_categories(tenant_id, profile.categories)
    tenant_categories = {*created, *existing, *extra}
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
        config = build_config(
            profile,
            reception_phone=reception_phone,
            previous=previous,
            reception_room_dial=reception_room_dial,
            escalation_category_key=escalation_category_key,
        )
        _require_escalation_category(
            config, tenant_categories, carried_over=escalation_category_key is None
        )
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


def _require_escalation_category(
    config: TenantConfig, tenant_categories: set[str], *, carried_over: bool
) -> None:
    """Служба эскалаций обязана быть категорией этого отеля — иначе отказ ДО записи.

    В рантайме такой ключ не роняет ход гостя (spec 0039 §2.3): заявка не
    заводится, в журнал уходит `escalation_request_skipped`. Но у отеля без бота
    это и есть тихий отказ — ЧП не видно ни в Telegram, ни в кабинете, — поэтому
    опечатку ловит онбординг, а не дежурный по логам. Ключ, перенесённый из
    прежнего конфига, проверяется так же: категорию могли удалить руками, — но
    отказ тогда говорит о прежнем конфиге, а не об опечатке во флаге, которого
    оператор не передавал.
    """
    key = config.escalation_category_key
    if key is None or key in tenant_categories:
        return
    known = ", ".join(sorted(tenant_categories))
    if carried_over:
        raise OnboardingError(
            f"Службы эскалаций «{key}» из прежнего конфига у отеля больше нет: "
            f"эскалации не заводят заявку. Передайте --escalation-category с ключом "
            f"существующей службы; службы отеля: {known}"
        )
    raise OnboardingError(
        f"Категории «{key}» у отеля нет: эскалации не смогли бы завести заявку. "
        f"Проверьте --escalation-category на опечатку; категории отеля: {known}"
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
            "перехвата без строки с номером; задаётся --reception-phone"
        )
    if config.reception_room_dial:
        lines.append(f"Ресепшен с телефона в номере: {config.reception_room_dial}")
    else:
        lines.append(
            "Номер ресепшена с телефона в номере НЕ задан: текст ЧП-перехвата "
            "скажет «позвоните с телефона в номере» без цифры; задаётся "
            "--reception-room-dial"
        )
    if config.escalation_category_key:
        lines.append(
            f"Служба эскалаций: {config.escalation_category_key} — ЧП и сбои бота "
            f"заводят в ней заявку в кабинете"
        )
    else:
        lines.append(
            "Служба эскалаций НЕ задана: ЧП и сбои бота заявку не заводят, в "
            "кабинете их не видно; задаётся --escalation-category"
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
    parser.add_argument(
        "--reception-room-dial",
        help="Что набрать на телефоне в номере, чтобы попасть на ресепшен, — например 0 "
        "(текст ЧП-перехвата, spec 0034 §3).",
    )
    parser.add_argument(
        "--escalation-category",
        help="Ключ службы, в которой ЧП и сбои бота заводят заявку, — например reception "
        "(spec 0039 §2.3). Не передан — остаётся прежний.",
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
                reception_room_dial=args.reception_room_dial,
                escalation_category_key=args.escalation_category,
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
