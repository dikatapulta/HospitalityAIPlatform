"""CLI онбординга отеля: тенант + категории служб + конфиг из профиля (#123).

Покрыты обе стороны задачи: сама команда (создание, идемпотентность, отказы,
что она НЕ трогает) и файл профиля пилотного отеля — опечатка в данных обязана
падать в CI, а не на живом отеле. Нужен работающий Postgres, как всем DB-тестам.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from hospitality.modules.requests.api import RequestCategoryCreate, create_category, list_categories
from hospitality.platform.config import (
    HotelFact,
    TenantConfig,
    load_tenant_config,
    store_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.tenancy import tenant_context
from hospitality.tools.onboard_tenant import (
    OnboardingError,
    OnboardingResult,
    TenantProfile,
    build_config,
    load_profile,
    main,
    onboard_tenant,
)

pytestmark = pytest.mark.usefixtures("canonical_database")

PILOT_PROFILE_PATH = Path(__file__).resolve().parents[1] / "ops" / "onboarding" / "pilot-hotel.json"

_SLUG = "pilot-hotel-test"


def _profile_data() -> dict[str, Any]:
    return {
        "city": "Almaty",
        "country_code": "KZ",
        "timezone": "Asia/Almaty",
        "default_language": "ru",
        "request_reminder_after_minutes": 30,
        "categories": [
            {
                "key": "housekeeping",
                "name": "Уборка и бельё",
                "reminder_after_minutes": 30,
                "hints": "кофе в пакетиках, вода",
            },
            {"key": "fnb", "name": "Room service", "reminder_after_minutes": 10},
        ],
    }


def _profile(**overrides: Any) -> TenantProfile:
    return TenantProfile.model_validate({**_profile_data(), **overrides})


async def _stored_config(slug: str = _SLUG) -> TenantConfig:
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == slug))
        assert tenant_id is not None
        return await load_tenant_config(session, tenant_id)


# --- профиль пилотного отеля: данные под присмотром CI ----------------------


def test_pilot_profile_matches_schema_and_eight_services() -> None:
    """Шесть служб пилота (issue #123) и две с 25.10 (#475): швейцар и банкетный
    зал — решение основателя."""
    profile = load_profile(PILOT_PROFILE_PATH)

    assert {category.key for category in profile.categories} == {
        "housekeeping",
        "maintenance",
        "it-support",
        "reception",
        "fnb",
        "laundry",
        "doorman",
        "banquet",
    }


def test_pilot_profile_sets_reminder_minutes_for_every_service() -> None:
    """Срок задан осознанно у КАЖДОЙ службы: иначе кабинет пометит просроченным
    всё, что висит дольше базовых 30 минут (spec 0028)."""
    profile = load_profile(PILOT_PROFILE_PATH)

    minutes = {category.key: category.reminder_after_minutes for category in profile.categories}
    assert minutes == {
        "reception": 10,
        "fnb": 10,
        "maintenance": 15,
        "housekeeping": 30,
        "it-support": 30,
        "laundry": 60,
        "doorman": 10,
        "banquet": 60,
    }


def test_pilot_profile_gives_doorman_safe_taxi_and_tv_setup() -> None:
    """Раздел основателя 25.10 (#475): телевизор настраивает швейцар, сломанный —
    IT, поэтому слово стоит у обеих (канон «кофе» ниже). Сейф и такси — только у
    швейцара: второй адрес у того же предмета модель выбирала бы наугад."""
    profile = load_profile(PILOT_PROFILE_PATH)

    def having(word: str) -> set[str]:
        return {c.key for c in profile.categories if c.hints and word in c.hints}

    assert having("телевизор") == {"doorman", "it-support"}
    assert having("сейф") == {"doorman"}
    assert having("такси") == {"doorman"}


def test_pilot_profile_names_coffee_in_two_services() -> None:
    """Живой случай 31.07 («2 пачки кофе»): кофе в пакетиках выдаёт housekeeping,
    сваренный — F&B. Предмет обязан стоять у ОБЕИХ служб, иначе модель угадывает."""
    profile = load_profile(PILOT_PROFILE_PATH)

    with_coffee = {c.key for c in profile.categories if c.hints and "кофе" in c.hints}
    assert with_coffee == {"housekeeping", "fnb"}


def test_pilot_profile_hints_pass_config_schema() -> None:
    """Профиль целиком проходит схему конфига (длина подсказок, формат ключей):
    опечатка в данных обязана падать в CI, а не на живом отеле."""
    profile = load_profile(PILOT_PROFILE_PATH)

    config = build_config(profile, reception_phone=None, previous=None)

    assert len(config.category_hints) == len(profile.categories)
    assert config.min_reminder_delay() == timedelta(minutes=10)


def test_load_profile_rejects_unknown_field(tmp_path: Path) -> None:
    """Опечатка в имени поля — отказ, а не молча проигнорированная настройка."""
    path = tmp_path / "hotel.json"
    path.write_text(json.dumps({**_profile_data(), "timezon": "Asia/Almaty"}), encoding="utf-8")

    with pytest.raises(OnboardingError, match="схеме профиля"):
        load_profile(path)


def test_load_profile_requires_explicit_base_reminder(tmp_path: Path) -> None:
    """Забытая строка не должна означать «напоминания выключены»: это решение."""
    data = _profile_data()
    del data["request_reminder_after_minutes"]
    path = tmp_path / "hotel.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(OnboardingError, match="схеме профиля"):
        load_profile(path)


def test_load_profile_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OnboardingError, match="Не читается"):
        load_profile(tmp_path / "no-such-file.json")


def test_load_profile_rejects_data_that_schema_of_config_forbids(tmp_path: Path) -> None:
    """Отказ схемы конфига (подсказка длиннее предела, кривой пояс) обязан
    случиться ДО обращения к БД: иначе тенант и категории уже созданы."""
    data = _profile_data()
    data["categories"][0]["hints"] = "к" * 500
    path = tmp_path / "hotel.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(OnboardingError, match="валидный конфиг тенанта"):
        load_profile(path)


def test_build_config_rejects_too_long_reception_phone() -> None:
    """Телефон приходит аргументом команды — его отказ тоже текстом, не трассировкой."""
    with pytest.raises(OnboardingError, match="валидный конфиг тенанта"):
        build_config(_profile(), reception_phone="+7" * 40, previous=None)


@pytest.mark.parametrize("dial", ["0a", "+0", "0 ", "1234567"])
def test_build_config_rejects_non_digit_room_dial(dial: str) -> None:
    """Цифра из номера встаёт в фразу «наберите {…}» (spec 0034 §3) — только цифры."""
    with pytest.raises(OnboardingError, match="валидный конфиг тенанта"):
        build_config(_profile(), reception_phone=None, previous=None, reception_room_dial=dial)


def test_build_config_takes_auto_checkout_grace_from_profile() -> None:
    """Запас автовыезда (spec 0043 §2.1) — из профиля: повторный онбординг его не
    сотрёт, а профиль без поля (до #225) даёт платформенные 2 часа, не «выключено»."""
    assert (
        build_config(_profile(), reception_phone=None, previous=None).stay_auto_checkout_grace_hours
        == 2
    )
    configured = build_config(
        _profile(stay_auto_checkout_grace_hours=6), reception_phone=None, previous=None
    )
    assert configured.stay_auto_checkout_grace_hours == 6
    disabled = build_config(
        _profile(stay_auto_checkout_grace_hours=None), reception_phone=None, previous=configured
    )
    assert disabled.stay_auto_checkout_grace() is None
    assert load_profile(PILOT_PROFILE_PATH).stay_auto_checkout_grace_hours == 2


def _room_service_data(category_key: str = "fnb", *, enabled: bool = True) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "catalogs": {
            "restaurant": {
                "category_key": category_key,
                "service_charge_percent": 10,
                "delivery_note": {"en": "Cooked after payment, 30–40 min"},
            }
        },
    }


def test_build_config_takes_room_service_from_profile() -> None:
    config = build_config(
        _profile(room_service=_room_service_data()), reception_phone=None, previous=None
    )

    restaurant = config.room_service_catalog("restaurant")
    assert restaurant is not None
    assert (restaurant.category_key, restaurant.service_charge_percent) == ("fnb", Decimal(10))
    assert config.room_service_catalog("shop") is None


def test_load_profile_rejects_room_service_with_unknown_service(tmp_path: Path) -> None:
    """spec 0038 §6: служба каталога не из категорий отеля — ошибка конфига ДО базы:
    заказ ушёл бы в службу, которой у отеля нет."""
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps({**_profile_data(), "room_service": _room_service_data("kitchen")}),
        encoding="utf-8",
    )

    with pytest.raises(OnboardingError, match="служба 'kitchen'"):
        load_profile(path)


# --- команда ----------------------------------------------------------------


async def test_onboarding_creates_tenant_categories_and_config() -> None:
    """DoD #123: у тенанта появляются службы, сроки, телефон и подсказки."""
    result = await onboard_tenant(
        slug=_SLUG, name="Pilot Hotel", profile=_profile(), reception_phone="+7 727 000 00 00"
    )

    assert result.tenant_created is True
    assert sorted(result.categories_created) == ["fnb", "housekeeping"]
    config = await _stored_config()
    assert config.reception_phone == "+7 727 000 00 00"
    assert config.request_reminder_minutes_by_category == {"housekeeping": 30, "fnb": 10}
    assert config.category_hints == {"housekeeping": "кофе в пакетиках, вода"}
    with tenant_context(result.tenant_id):
        assert [category.key for category in await list_categories()] == ["fnb", "housekeeping"]


async def test_onboarding_is_idempotent() -> None:
    """Повторный запуск ничего не дублирует: команда живёт на живом отеле."""
    first = await onboard_tenant(
        slug=_SLUG,
        name="Pilot Hotel",
        profile=_profile(),
        reception_phone="+7 727 000 00 00",
        reception_room_dial="0",
    )

    second = await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    assert second.tenant_id == first.tenant_id
    assert second.tenant_created is False
    assert second.categories_created == ()
    assert sorted(second.categories_existing) == ["fnb", "housekeeping"]
    # Телефон и цифра не переданы повторно — прежние сохранены, а не стёрты.
    assert (await _stored_config()).reception_phone == "+7 727 000 00 00"
    assert (await _stored_config()).reception_room_dial == "0"


async def test_onboarding_keeps_escalation_category_of_existing_tenant() -> None:
    """spec 0039 §2.3: служба эскалаций переживает повторный онбординг без флага —
    стёртая молча, она вернула бы отелю без бота эскалации в никуда."""
    await onboard_tenant(
        slug=_SLUG,
        name="Pilot Hotel",
        profile=_profile(),
        reception_phone=None,
        escalation_category_key="housekeeping",
    )

    await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    assert (await _stored_config()).escalation_category_key == "housekeeping"


async def test_onboarding_rejects_escalation_category_the_hotel_does_not_have() -> None:
    """Опечатка в `--escalation-category` падает на онбординге, а не тихим
    `escalation_request_skipped` в рантайме: у отеля без бота это пропавшее ЧП.
    Конфиг при этом не записывается."""
    with pytest.raises(OnboardingError, match="Категории «recepton» у отеля нет"):
        await onboard_tenant(
            slug=_SLUG,
            name="Pilot Hotel",
            profile=_profile(),
            reception_phone=None,
            escalation_category_key="recepton",
        )

    async with platform_session_scope() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == _SLUG))
    assert tenant is not None
    assert tenant.config is None


async def test_carried_escalation_category_without_its_service_is_named_as_such() -> None:
    """Службу из прежнего конфига удалили руками — повторный онбординг без флага
    отказывает, но говорит о прежнем конфиге, а не об опечатке во флаге, которого
    оператор не передавал."""
    await onboard_tenant(
        slug=_SLUG,
        name="Pilot Hotel",
        profile=_profile(),
        reception_phone=None,
        escalation_category_key="housekeeping",
    )
    config = await _stored_config()
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == _SLUG))
        assert tenant_id is not None
        await store_tenant_config(
            session, tenant_id, config.model_copy(update={"escalation_category_key": "security"})
        )

    with pytest.raises(OnboardingError, match="«security» из прежнего конфига"):
        await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)


async def test_onboarding_keeps_staff_chats_of_existing_tenant() -> None:
    """Чаты служб задаёт отдельная операция (`tools/staff_routing`) — онбординг
    их переносит как есть, иначе повторный запуск обрывает уведомления."""
    await onboard_tenant(slug=_SLUG, name="Pilot Hotel", profile=_profile(), reception_phone=None)
    config = await _stored_config()
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == _SLUG))
        assert tenant_id is not None
        await store_tenant_config(
            session, tenant_id, config.model_copy(update={"staff_chats_by_category": {"fnb": "-1"}})
        )

    await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    assert (await _stored_config()).staff_chats_by_category == {"fnb": "-1"}


async def test_onboarding_keeps_daily_summary_settings_of_existing_tenant() -> None:
    """Настройки утренней сводки задаёт отдельная операция (`tools/daily_summary`,
    spec 0035 §8) — онбординг их переносит как есть.

    Без переноса правка профиля отеля молча выключала бы утреннее сообщение:
    чат обнулился бы, а менеджер узнал бы об этом, не получив сводку.
    """
    await onboard_tenant(slug=_SLUG, name="Pilot Hotel", profile=_profile(), reception_phone=None)
    config = await _stored_config()
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == _SLUG))
        assert tenant_id is not None
        await store_tenant_config(
            session,
            tenant_id,
            config.model_copy(
                update={"daily_summary_chat_id": "-100777", "daily_summary_local_time": "08:15"}
            ),
        )

    await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    stored = await _stored_config()
    assert stored.daily_summary_chat_id == "-100777"
    assert stored.daily_summary_local_time == "08:15"


async def test_onboarding_keeps_hotel_facts_of_existing_tenant() -> None:
    """Справочник отеля пишет менеджер, а не файл профиля (spec 0036 §10) —
    онбординг переносит его как есть.

    Без переноса повторный онбординг (документированная правка профиля отеля,
    `docs/runbooks/tenant-onboarding.md`) молча стирал бы до 30 фактов, которые
    менеджер набирал руками: узнали бы об этом по тому, что бот перестал знать
    про Wi-Fi.
    """
    await onboard_tenant(slug=_SLUG, name="Pilot Hotel", profile=_profile(), reception_phone=None)
    config = await _stored_config()
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == _SLUG))
        assert tenant_id is not None
        await store_tenant_config(
            session,
            tenant_id,
            config.model_copy(
                update={"hotel_facts": [HotelFact(topic="Wi-Fi", answer="Сеть Grand-Guest")]}
            ),
        )

    await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    stored = await _stored_config()
    assert [fact.topic for fact in stored.hotel_facts] == ["Wi-Fi"], (
        f"справочник стёрт повторным онбордингом: {stored.hotel_facts}"
    )


async def test_onboarding_keeps_room_service_of_existing_tenant() -> None:
    """spec 0038 §6: профиль без `room_service` (публичный, без сбора пилота) не
    стирает настроенный room service; явный `"enabled": false` — выключает."""
    with_room_service = _profile(room_service=_room_service_data())
    await onboard_tenant(
        slug=_SLUG, name="Pilot Hotel", profile=with_room_service, reception_phone=None
    )

    await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    assert (await _stored_config()).room_service == with_room_service.room_service

    disabled = _profile(room_service=_room_service_data(enabled=False))
    await onboard_tenant(slug=_SLUG, name=None, profile=disabled, reception_phone=None)

    assert (await _stored_config()).room_service_catalog("restaurant") is None


async def test_onboarding_keeps_mentor_flag_of_existing_tenant() -> None:
    """Раздел «Наставник» включают руками (spec 0048 §2) — повторный онбординг его не выключает."""
    await onboard_tenant(slug=_SLUG, name="Pilot Hotel", profile=_profile(), reception_phone=None)
    config = await _stored_config()
    assert config.mentor_enabled is False
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == _SLUG))
        assert tenant_id is not None
        await store_tenant_config(
            session, tenant_id, config.model_copy(update={"mentor_enabled": True})
        )

    await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    assert (await _stored_config()).mentor_enabled is True


async def test_onboarding_reports_categories_outside_profile() -> None:
    """Лишнюю категорию онбординг не удаляет (за ней могут стоять заявки), но
    обязан о ней сказать: она остаётся живым адресом доставки."""
    result = await onboard_tenant(
        slug=_SLUG, name="Pilot Hotel", profile=_profile(), reception_phone=None
    )
    with tenant_context(result.tenant_id):
        await create_category(RequestCategoryCreate(key="spa", name="СПА"))

    again = await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    assert again.categories_extra == ("spa",)


async def test_onboarding_refuses_to_create_tenant_without_name() -> None:
    """Опечатка в --slug не должна молча плодить пустых тенантов."""
    with pytest.raises(OnboardingError, match="--name"):
        await onboard_tenant(slug="typo-hotel", name=None, profile=_profile(), reception_phone=None)

    async with platform_session_scope() as session:
        assert await session.scalar(select(Tenant.id).where(Tenant.slug == "typo-hotel")) is None


def _stub_onboarding(
    monkeypatch: pytest.MonkeyPatch, result: OnboardingResult | OnboardingError
) -> list[dict[str, Any]]:
    """Подменить работу с БД заглушкой; вернуть журнал вызовов.

    `main` синхронный и сам зовёт `asyncio.run` — в БД он в тесте не ходит
    (чужой event loop), как в тестах `staff_routing`: здесь проверяются разбор
    аргументов и отчёт оператору, а работа с БД покрыта тестами выше.
    """
    calls: list[dict[str, Any]] = []

    async def fake(**kwargs: Any) -> OnboardingResult:
        calls.append(kwargs)
        if isinstance(result, OnboardingError):
            raise result
        return result

    monkeypatch.setattr("hospitality.tools.onboard_tenant.onboard_tenant", fake)
    return calls


def _pilot_result() -> OnboardingResult:
    profile = load_profile(PILOT_PROFILE_PATH)
    return OnboardingResult(
        tenant_id=uuid.uuid4(),
        tenant_created=True,
        categories_created=tuple(category.key for category in profile.categories),
        categories_existing=(),
        categories_extra=(),
        config=build_config(
            profile,
            reception_phone="+7 727 000 00 00",
            previous=None,
            reception_room_dial="0",
            escalation_category_key="reception",
        ),
    )


def test_main_passes_arguments_and_prints_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Сквозной путь оператора: аргументы → отчёт, в котором видно главное."""
    calls = _stub_onboarding(monkeypatch, _pilot_result())

    code = main(
        [
            str(PILOT_PROFILE_PATH),
            "--slug",
            _SLUG,
            "--name",
            "Pilot Hotel",
            "--reception-phone",
            "+7 727 000 00 00",
            "--reception-room-dial",
            "0",
            "--escalation-category",
            "reception",
        ]
    )

    assert code == 0
    assert calls[0]["slug"] == _SLUG
    assert calls[0]["reception_phone"] == "+7 727 000 00 00"
    assert calls[0]["reception_room_dial"] == "0"
    assert calls[0]["escalation_category_key"] == "reception"
    report = capsys.readouterr().out
    assert "создан" in report
    assert "+7 727 000 00 00" in report
    assert "Ресепшен с телефона в номере: 0" in report
    assert "Служба эскалаций: reception" in report
    assert "laundry: 60 мин" in report
    assert "housekeeping: уборка номера" in report


def test_main_warns_when_reception_phone_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Без телефона неавторизованный гость в веб-чате получит отказ «в никуда»
    (spec 0027 §3.1) — отчёт обязан это назвать, а не промолчать."""
    profile = load_profile(PILOT_PROFILE_PATH)
    result = _pilot_result().model_copy(
        update={"config": build_config(profile, reception_phone=None, previous=None)}
    )
    _stub_onboarding(monkeypatch, result)

    assert main([str(PILOT_PROFILE_PATH), "--slug", _SLUG]) == 0

    report = capsys.readouterr().out
    assert "Телефон ресепшена НЕ задан" in report
    assert "Номер ресепшена с телефона в номере НЕ задан" in report
    assert "Служба эскалаций НЕ задана" in report


def test_main_reports_input_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_onboarding(monkeypatch, OnboardingError("Тенанта со slug 'typo-hotel' нет … --name"))

    code = main([str(PILOT_PROFILE_PATH), "--slug", "typo-hotel"])

    assert code == 1
    assert "--name" in capsys.readouterr().err
