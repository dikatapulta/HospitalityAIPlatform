"""CLI онбординга отеля: тенант + категории служб + конфиг из профиля (#123).

Покрыты обе стороны задачи: сама команда (создание, идемпотентность, отказы,
что она НЕ трогает) и файл профиля пилотного отеля — опечатка в данных обязана
падать в CI, а не на живом отеле. Нужен работающий Postgres, как всем DB-тестам.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from hospitality.modules.requests.api import RequestCategoryCreate, create_category, list_categories
from hospitality.platform.config import TenantConfig, load_tenant_config, store_tenant_config
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


def test_pilot_profile_matches_schema_and_six_services() -> None:
    """Шесть служб пилота (issue #123): ресепшен, F&B и прачечная — те самые,
    которых нет в демо-сиде и которым сегодня некуда приземляться."""
    profile = load_profile(PILOT_PROFILE_PATH)

    assert {category.key for category in profile.categories} == {
        "housekeeping",
        "maintenance",
        "it-support",
        "reception",
        "fnb",
        "laundry",
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
    }


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
        slug=_SLUG, name="Pilot Hotel", profile=_profile(), reception_phone="+7 727 000 00 00"
    )

    second = await onboard_tenant(slug=_SLUG, name=None, profile=_profile(), reception_phone=None)

    assert second.tenant_id == first.tenant_id
    assert second.tenant_created is False
    assert second.categories_created == ()
    assert sorted(second.categories_existing) == ["fnb", "housekeeping"]
    # Телефон не передан повторно — прежний сохранён, а не стёрт.
    assert (await _stored_config()).reception_phone == "+7 727 000 00 00"


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
        config=build_config(profile, reception_phone="+7 727 000 00 00", previous=None),
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
        ]
    )

    assert code == 0
    assert calls[0]["slug"] == _SLUG
    assert calls[0]["reception_phone"] == "+7 727 000 00 00"
    report = capsys.readouterr().out
    assert "создан" in report
    assert "+7 727 000 00 00" in report
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

    assert "Телефон ресепшена НЕ задан" in capsys.readouterr().out


def test_main_reports_input_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_onboarding(monkeypatch, OnboardingError("Тенанта со slug 'typo-hotel' нет … --name"))

    code = main([str(PILOT_PROFILE_PATH), "--slug", "typo-hotel"])

    assert code == 1
    assert "--name" in capsys.readouterr().err
