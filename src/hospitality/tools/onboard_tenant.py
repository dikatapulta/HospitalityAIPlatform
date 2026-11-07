"""Онбординг отеля: тенант + категории служб + конфиг из файла профиля (#123).

Канонический путь «подключить настоящий отель» (§6 «Онбординг отеля»). Отличие
от `tools/seed.py`: сид создаёт ДЕМО-данные dev/CI одного фиксированного состава
(три категории Demo Hotel) и бежит на каждом деплое; онбординг — осознанное
разовое действие оператора над конкретным отелем, состав служб берётся из файла
профиля, а не из кода (P-11: различия отелей — данные).

Что делает (идемпотентно, повторный запуск безопасен):

1. тенанта со `--tenant-slug` создаёт, если его нет (нужен `--name`), иначе берёт;
2. категории из профиля — создаёт недостающие, существующие не трогает
   (переименование службы — отдельное осознанное действие, не онбординг);
3. конфиг тенанта пересобирает из профиля и пишет каноническим путём
   (`store_tenant_config`): пояс, язык, телефон ресепшена, сроки напоминаний
   (spec 0028), подсказки служб (`category_hints`), службу, в которой
   эскалация заводит заявку (`--escalation-category`, spec 0039 §2.3; выбор
   обязателен — служба или `--escalation-telegram-only`, иначе отказ, #786),
   room service (`room_service`, spec 0038 Р-10), квоты бесплатного
   (`service_quotas`, spec 0056 §3) и список номеров отеля (`hotel_rooms`,
   spec 0061 §2).

Чаты НЕ трогаются: и `staff_chats_by_category` (`tools/staff_routing`), и пара
настроек утренней сводки `daily_summary_chat_id` / `daily_summary_local_time`
(`tools/daily_summary`, spec 0035 §8) переносятся из прежнего конфига — их id
зависят от инсталляции, а не от отеля, и узнают их, добавив бота в группу.

Запуск (локально; на staging — то же внутри контейнера, префиксом
`docker compose -f /opt/hospitality/docker-compose.staging.yml exec app`):

    python -m hospitality.tools.onboard_tenant ops/onboarding/pilot-hotel.json \\
        --tenant-slug <код-отеля> --name "Название отеля" --reception-phone "+7 727 000 00 00" \\
        --escalation-category reception

    python -m hospitality.tools.onboard_tenant ops/onboarding/pilot-hotel.json \\
        --tenant-slug demo-hotel     # репетиция профиля пилота на demo (staging)

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
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.requests.api import (
    ERR_REQUESTS_CATEGORY_KEY_TAKEN,
    RequestCategoryCreate,
    create_category,
    list_categories,
)
from hospitality.platform.config import (
    DEFAULT_DAILY_SUMMARY_LOCAL_TIME,
    DEFAULT_GUEST_REMINDER_RAISE_AFTER,
    DEFAULT_MENTOR_EXAM_PASS_PERCENT,
    DEFAULT_MENTOR_EXAM_QUESTION_COUNT,
    DEFAULT_PMS_SNAPSHOT_INTERVAL_MINUTES,
    DEFAULT_STAY_AUTO_CHECKOUT_GRACE_HOURS,
    DEFAULT_STAY_CHECKOUT_HOUR,
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    HotelProfile,
    PmsSource,
    RoomServiceConfig,
    TenantConfig,
    load_tenant_config,
    store_tenant_config,
)
from hospitality.platform.guest_chat_look import GuestChatLook
from hospitality.platform.hotel_rooms import HotelRoom
from hospitality.platform.models import Tenant
from hospitality.platform.service_quotas import QUOTA_PRICE_MINOR_PER_TENGE, ServiceQuota
from hospitality.platform.showroom_config import ShowroomConfig
from hospitality.platform.table_booking_config import TableBookingConfig
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
    # Квоты бесплатного (spec 0056 §3): норма на гостя в день и платное сверх.
    # Нет ключа — прежние квоты переносятся, как у `room_service`: норма и цена
    # пилота живут только в файле `*.local`, и прогон публичного профиля не
    # должен ни стирать, ни включать их сам. Убрать все — `"service_quotas": []`.
    service_quotas: tuple[ServiceQuota, ...] | None = None
    # Бронь столика (spec 0064 §2.6): рестораны, их службы и часы. Нет ключа —
    # прежнее значение, как у `room_service`: рестораны пилота — в файле `*.local`.
    # Выключить — `"enabled": false`.
    table_booking: TableBookingConfig | None = None
    # 3D-витрина (spec 0069 §2.5): флаги и службы запросов. Нет ключа — прежнее
    # значение, как у `room_service`. Кода доступа в профиле нет: его ставит команда
    # `showroom_access_code`, а онбординг переносит хэш и версию из прежнего конфига.
    showroom: ShowroomConfig | None = None
    # Расчётный час и источник проживаний (spec 0058 §3). Нет ключа — прежнее
    # значение, как у `room_service`: прогон профиля без этих строк не вправе
    # вернуть отель с выгрузкой Opera на ручное заселение или сдвинуть его час.
    stay_checkout_hour: int | None = None
    pms_source: PmsSource | None = None
    pms_snapshot_interval_minutes: int | None = None
    # Номера отеля для доски «Номера» (spec 0061 §2). Нет ключа — прежний список,
    # как у `service_quotas`: номера пилота — только в `*.local`, публичный профиль
    # их не несёт. Убрать все — `"hotel_rooms": []`.
    hotel_rooms: tuple[HotelRoom, ...] | None = None


class OnboardingResult(BaseModel):
    """Что получилось — для отчёта оператору и для тестов."""

    model_config = ConfigDict(extra="forbid", frozen=True)

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
    escalation_telegram_only: bool = False,
) -> TenantConfig:
    """Собрать конфиг тенанта из профиля (P-12: запись — через `store_tenant_config`).

    Профиль задаёт конфиг ЦЕЛИКОМ, кроме перечисленного ниже:
    `staff_chats_by_category`, пара настроек утренней сводки (`daily_summary_chat_id` /
    `daily_summary_local_time`), справочник отеля `hotel_facts`, флаг раздела
    «Наставник» `mentor_enabled` (spec 0048 §2) с настройками его экзамена (§12), флаг
    раздела «Предложить идею» `ideas_enabled` (spec 0071 §2), вид
    гостевого чата `guest_chat_look` (spec 0066 §3), сроки «долго в работе»
    `request_stalled_*` (spec 0028 §8) и порог подъёма по напоминаниям гостя
    `guest_reminder_raise_after` (spec 0070 §6) переносятся из прежнего конфига — это
    отдельные операции (`staff_routing`, `daily_summary`, `request_reminders --stalled`
    и `--guest-raise-after`,
    страница «Справочник отеля», включение раздела руками), потому что id чата
    узнают, добавив бота в группу, а факты пишет менеджер отеля, — ни то, ни
    другое не выписывают в файл профиля; телефон ресепшена, что набрать на
    ресепшен из номера и служба эскалаций (с выбором «только Telegram», #786)
    берутся из аргументов или, если их не передали, из прежнего конфига — чтобы
    повторный онбординг не стёр уже настроенное (служба эскалаций, стёртая
    молча, вернула бы отелю без бота эскалации в никуда — spec 0039 §2.3).

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
    профиля — иначе заказ ушёл бы в службу, которой у отеля нет. Квоты
    бесплатного (spec 0056 §3) и бронь столика (spec 0064 §2.6) — по тому же
    правилу и с той же проверкой служб. 3D-витрина (spec 0069 §2.5) — тоже, но код
    доступа (хэш и версия) всегда переносится из прежнего конфига.
    Расчётный час и источник проживаний (spec 0058 §3) и список номеров отеля
    (spec 0061 §2) — по тому же правилу.

    Схема конфига — последний рубеж проверки данных отеля (длина подсказки,
    формат ключа, пояс): её отказ превращается в текст оператору, а не в
    трассировку — команду запускает основатель, а не разработчик.
    """
    _check_room_service_categories(profile)
    _check_service_quota_categories(profile)
    _check_table_booking_categories(profile)
    _check_showroom(profile)
    escalation_key, telegram_only = _escalation_choice(
        previous, escalation_category_key, escalation_telegram_only=escalation_telegram_only
    )
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
            ideas_enabled=previous.ideas_enabled if previous else False,
            mentor_exam_question_count=(
                previous.mentor_exam_question_count
                if previous
                else DEFAULT_MENTOR_EXAM_QUESTION_COUNT
            ),
            mentor_exam_pass_percent=(
                previous.mentor_exam_pass_percent if previous else DEFAULT_MENTOR_EXAM_PASS_PERCENT
            ),
            reception_phone=reception_phone or (previous.reception_phone if previous else None),
            reception_room_dial=(
                reception_room_dial or (previous.reception_room_dial if previous else None)
            ),
            escalation_category_key=escalation_key,
            escalation_telegram_only=telegram_only,
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
            guest_reminder_raise_after=(
                previous.guest_reminder_raise_after
                if previous
                else DEFAULT_GUEST_REMINDER_RAISE_AFTER
            ),
            category_hints={
                category.key: category.hints for category in profile.categories if category.hints
            },
            room_service=(
                profile.room_service
                if profile.room_service is not None
                else (previous.room_service if previous else None)
            ),
            service_quotas=(
                profile.service_quotas
                if profile.service_quotas is not None
                else (previous.service_quotas if previous else ())
            ),
            table_booking=(
                profile.table_booking
                if profile.table_booking is not None
                else (previous.table_booking if previous else None)
            ),
            showroom=_showroom_with_previous_code(
                profile.showroom, previous.showroom if previous else None
            ),
            stay_checkout_hour=_from_profile_or_previous(
                profile.stay_checkout_hour,
                previous.stay_checkout_hour if previous else None,
                DEFAULT_STAY_CHECKOUT_HOUR,
            ),
            pms_source=_from_profile_or_previous(
                profile.pms_source, previous.pms_source if previous else None, "manual"
            ),
            pms_snapshot_interval_minutes=_from_profile_or_previous(
                profile.pms_snapshot_interval_minutes,
                previous.pms_snapshot_interval_minutes if previous else None,
                DEFAULT_PMS_SNAPSHOT_INTERVAL_MINUTES,
            ),
            hotel_rooms=_from_profile_or_previous(
                profile.hotel_rooms, previous.hotel_rooms if previous else None, ()
            ),
            guest_chat_look=previous.guest_chat_look if previous else GuestChatLook(),
        )
    except ValidationError as error:
        raise OnboardingError(
            f"Профиль и аргументы не дают валидный конфиг тенанта:\n{error}"
        ) from error


def _escalation_choice(
    previous: TenantConfig | None,
    escalation_category_key: str | None,
    *,
    escalation_telegram_only: bool,
) -> tuple[str | None, bool]:
    """Служба эскалаций и выбор «только Telegram» (#786): флаг — или прежний конфиг.

    Флаги взаимоисключающие (их разводит argparse); переданный флаг сбрасывает
    второе поле, иначе в конфиге остался бы противоречивый выбор.
    """
    if escalation_category_key:
        return escalation_category_key, False
    if escalation_telegram_only:
        return None, True
    if previous is None:
        return None, False
    return previous.escalation_category_key, previous.escalation_telegram_only


def _from_profile_or_previous[T](from_profile: T | None, from_previous: T | None, default: T) -> T:
    """Значение профиля; нет ключа — прежнего конфига; нет и его — умолчание."""
    if from_profile is not None:
        return from_profile
    return from_previous if from_previous is not None else default


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


def _check_table_booking_categories(profile: TenantProfile) -> None:
    """Служба каждого ресторана брони — категория из профиля (spec 0064 §2.6, §7 п. 7)."""
    if profile.table_booking is None:
        return
    known = {category.key for category in profile.categories}
    for restaurant in profile.table_booking.restaurants:
        if restaurant.category_key not in known:
            raise OnboardingError(
                f"table_booking: у ресторана {restaurant.key!r} служба "
                f"{restaurant.category_key!r}, а в categories профиля такой нет. Добавьте "
                f"службу в categories или исправьте ключ."
            )


def _check_showroom(profile: TenantProfile) -> None:
    """Службы витрины — категории из профиля; кода доступа в профиле нет (spec 0069 §2.5)."""
    showroom = profile.showroom
    if showroom is None:
        return
    if showroom.access_code_hash is not None or showroom.access_code_version != 0:
        raise OnboardingError(
            "showroom: код доступа файлом профиля не задаётся — уберите access_code_hash и "
            "access_code_version, код ставит команда "
            "`python -m hospitality.tools.showroom_access_code set`."
        )
    known = {category.key for category in profile.categories}
    for key in sorted(showroom.category_keys):
        if key not in known:
            raise OnboardingError(
                f"showroom: служба {key!r}, а в categories профиля такой нет. Добавьте "
                f"службу в categories или исправьте ключ."
            )


def _showroom_with_previous_code(
    from_profile: ShowroomConfig | None, previous: ShowroomConfig | None
) -> ShowroomConfig | None:
    """Витрина профиля с кодом доступа прежнего конфига; нет ключа в профиле — прежняя.

    Повторный онбординг не вправе ни стереть код (страница по коду погасла бы на
    показе), ни сбросить его версию (вернулись бы cookies прежнего кода).
    """
    if from_profile is None:
        return previous
    if previous is None:
        return from_profile
    return from_profile.model_copy(
        update={
            "access_code_hash": previous.access_code_hash,
            "access_code_version": previous.access_code_version,
        }
    )


def _check_service_quota_categories(profile: TenantProfile) -> None:
    """Обе службы каждой квоты — категории из профиля (spec 0056 §3).

    Иначе бесплатное или платное ушло бы в службу, которой у отеля нет, и
    заявка упала бы уже на ходе гостя.
    """
    if not profile.service_quotas:
        return
    known = {category.key for category in profile.categories}
    for quota in profile.service_quotas:
        services = [("бесплатного", quota.category_key)]
        if quota.paid is not None:
            services.append(("платного", quota.paid.category_key))
        for role, category_key in services:
            if category_key not in known:
                raise OnboardingError(
                    f"service_quotas: у квоты {quota.key!r} служба {role} {category_key!r}, "
                    f"а в categories профиля такой нет. Добавьте службу в categories или "
                    f"исправьте ключ."
                )


async def _ensure_tenant(slug: str, name: str | None) -> tuple[uuid.UUID, bool]:
    """Тенант со `slug`: вернуть его id и признак «создан этим запуском».

    Тенанта нет и имя не передано — отказ: опечатка в `--tenant-slug` не должна
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
    escalation_telegram_only: bool = False,
) -> OnboardingResult:
    """Онбординг отеля целиком: тенант → категории → конфиг (идемпотентно)."""
    if not escalation_category_key and not escalation_telegram_only:
        await _require_escalation_choice(slug, name, profile)
    tenant_id, tenant_created = await _ensure_tenant(slug, name)
    created, existing, extra = await _ensure_categories(tenant_id, profile.categories)
    tenant_categories = {*created, *existing, *extra}
    async with platform_session_scope() as session:
        previous = await _previous_config(session, tenant_id)
        config = build_config(
            profile,
            reception_phone=reception_phone,
            previous=previous,
            reception_room_dial=reception_room_dial,
            escalation_category_key=escalation_category_key,
            escalation_telegram_only=escalation_telegram_only,
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


async def _previous_config(session: AsyncSession, tenant_id: uuid.UUID) -> TenantConfig | None:
    """Прежний конфиг тенанта; `None` — онбординг ещё не был завершён (§6).

    Любая другая ошибка чтения (дрейф схемы, ERR-PLATFORM-006) — наружу: молча
    затирать конфиг, который мы не смогли прочитать, нельзя.
    """
    try:
        return await load_tenant_config(session, tenant_id)
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        return None


async def _require_escalation_choice(slug: str, name: str | None, profile: TenantProfile) -> None:
    """Без флага выбор службы эскалаций переносится из прежнего конфига; нет его — отказ.

    Отказ ДО записи (#786): новый отель не успевает получить тенанта и
    категории без конфига. Отель без выбора — тот, чей гость, которому нужен
    человек, виден только в Telegram, а кабинет молчит (находка #785).
    Тенанта нет и имени нет — это опечатка в slug: её называет `_ensure_tenant`.
    """
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
        previous = None if tenant_id is None else await _previous_config(session, tenant_id)
    if tenant_id is None and name is None:
        return
    if previous is not None and not previous.escalation_choice_missing:
        return
    services = ", ".join(category.key for category in profile.categories)
    raise OnboardingError(
        "Не выбрана служба эскалаций — куда попадёт гость, которому нужен человек "
        "(ЧП, «позовите сотрудника», бот не смог ответить). Передайте "
        "--escalation-category <ключ службы> — обычно reception: такие обращения станут "
        "заявками в кабинете. Отелю хватает Telegram — передайте "
        "--escalation-telegram-only: обращения уйдут только в чат Telegram, в кабинете "
        f"их не будет. Службы профиля: {services}"
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
    elif config.escalation_telegram_only:
        lines.append(
            "Служба эскалаций — только Telegram (--escalation-telegram-only): ЧП и сбои "
            "бота заявку не заводят, в кабинете их не видно; службу задаёт "
            "--escalation-category"
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
    lines.append(f"Квоты бесплатного: {len(config.service_quotas)}")
    for quota in config.service_quotas:
        lines.append(
            f"  {quota.key}: {quota.per_guest_per_day} на гостя в день, {quota.category_key}; "
            f"сверх — {_paid_report(quota)}"
        )
    lines.append(f"Номеров отеля в списке: {len(config.hotel_rooms)}")
    chats = len(config.staff_chats_by_category)
    lines.append(
        f"Чаты служб: {chats} (перенесены как были; задаются `python -m "
        f"hospitality.tools.staff_routing`)"
    )
    return "\n".join(lines)


def _paid_report(quota: ServiceQuota) -> str:
    """Строка отчёта о платном сверх нормы (spec 0056 Р-6)."""
    if quota.paid is None:
        return "платного нет, подскажут на ресепшене"
    price = quota.paid.unit_price_minor
    if price is None:
        return f"платно, {quota.paid.category_key}, цену назовёт сотрудник"
    return f"платно, {quota.paid.category_key}, {price // QUOTA_PRICE_MINOR_PER_TENGE} ₸ за штуку"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hospitality.tools.onboard_tenant",
        description="Онбординг отеля: тенант, категории служб, конфиг из файла профиля (#123).",
    )
    parser.add_argument("profile", type=Path, help="Файл профиля отеля (JSON).")
    parser.add_argument(
        "--tenant-slug",
        default=get_settings().telegram_tenant_slug,
        help="Тенант (по умолчанию — TELEGRAM_TENANT_SLUG).",
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
    escalation = parser.add_mutually_exclusive_group()
    escalation.add_argument(
        "--escalation-category",
        help="Ключ службы, в которой ЧП и сбои бота заводят заявку, — например reception "
        "(spec 0039 §2.3). Не передан ни он, ни --escalation-telegram-only — остаётся "
        "прежний выбор, а без него отказ (#786).",
    )
    escalation.add_argument(
        "--escalation-telegram-only",
        action="store_true",
        help="Отелю хватает Telegram: ЧП и сбои бота заявку не заводят, в кабинете их "
        "не видно (spec 0039 §2.3, #786).",
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
                slug=args.tenant_slug,
                name=args.name,
                profile=profile,
                reception_phone=args.reception_phone,
                reception_room_dial=args.reception_room_dial,
                escalation_category_key=args.escalation_category,
                escalation_telegram_only=args.escalation_telegram_only,
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
    print(format_report(args.tenant_slug, result))
    return 0


if __name__ == "__main__":  # pragma: no cover — точка входа; логика покрыта тестами
    raise SystemExit(main())
