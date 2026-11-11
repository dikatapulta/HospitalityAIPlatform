"""Политика пароля персонала (#486, #967): длина и «не угадывается первыми попытками».

Чистая функция `ensure_password_policy` — без БД; слова отеля из базы —
`load_hotel_words` ниже; двери (приглашение, ссылка нового пароля) проверяются
страничными тестами `staff_portal/tests` и `test_staff_invites.py`, бутстрап —
`test_staff_bootstrap_tool.py`. Отели и имена здесь выдуманные.
"""

from __future__ import annotations

import pytest

from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import Tenant
from hospitality.platform.staff_password_policy import (
    ERR_AUTH_PASSWORD_GUESSABLE,
    ERR_AUTH_PASSWORD_TOO_SHORT,
    ensure_password_policy,
    load_hotel_words,
)
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError

# Выдуманный отель: название, код, город, страна — как их отдаёт `load_hotel_words`.
HOTEL = ("Отель «Самал Тау»", "samal-tau", "Талдыкорган", "казахстан", "қазақстан")


def _rejected(password: str, *, personal: tuple[str, ...] = (), hotel: tuple[str, ...] = ()) -> str:
    with pytest.raises(AppError) as error:
        ensure_password_policy(password, personal=personal, hotel=hotel)
    assert error.value.status_code == 422
    return error.value.code


@pytest.mark.parametrize(
    "password",
    [
        # Пробы аудита (#486): все пять были приняты.
        "12345678",
        "password",
        "qwerty123",
        "Password1",
        # Частый пароль «словом в середине» и из русского списка (`пароль` в en-раскладке).
        "Dragon2026!",
        "gfhjkm12",
        # Ряды клавиатуры и алфавита, в обе стороны, латиница и йцукен.
        "87654321",
        "asdf1234",
        "ЙЦУКЕНГШ",
        "abcdefgh",
        # Повтор одного куска.
        "BORMBORM",
        "12121212",
        "        ",
    ],
)
def test_guessable_password_rejected(password: str) -> None:
    assert _rejected(password) == ERR_AUTH_PASSWORD_GUESSABLE


@pytest.mark.parametrize(
    "password",
    [
        # Телефон и дата рождения в любой записи (#967).
        "87011234567",
        "+7 701 123 45 67",
        "12.05.1990",
        "1990-05-12",
        "4!7#9$2%",
    ],
)
def test_password_without_letters_rejected(password: str) -> None:
    assert _rejected(password) == ERR_AUTH_PASSWORD_GUESSABLE


@pytest.mark.parametrize(
    "password",
    [
        # Слова любого отеля (#967): кириллицей, латиницей, в чужой раскладке.
        "Пароль2026",
        "Parol2026!",
        "gfhjkm-2026",
        "Necturn2026",
        "Hotel2026!",
        "Отель-2026",
    ],
)
def test_generic_hotel_words_rejected(password: str) -> None:
    assert _rejected(password) == ERR_AUTH_PASSWORD_GUESSABLE


@pytest.mark.parametrize(
    "password",
    [
        # Название отеля: как написано, латиницей, в en-раскладке (`Самал` — `cfvfk`).
        "Самал2026",
        "Samal2026!",
        "cfvfk2026",
        "samaltau",
        # Код отеля и город — кириллицей и в любой ходовой латинской записи.
        "SAMAL-TAU-1",
        "Талдыкорган2026",
        "Taldykorgan1",
        "Taldiqorgan!1",
        # Страна — по-русски, по-казахски и обеими латинскими записями.
        "Казахстан2026",
        "Kazakhstan2026",
        "Kazahstan!",
        "Qazaqstan2026",
        "Қазақстан2026",
    ],
)
def test_hotel_and_place_words_rejected(password: str) -> None:
    assert _rejected(password, hotel=HOTEL) == ERR_AUTH_PASSWORD_GUESSABLE


@pytest.mark.parametrize(
    ("password", "personal"),
    [
        ("borm-hotel-7", ("BORM", "Борис Мельников")),
        ("Melnikov!2026", ("BMEL", "Boris Melnikov")),
        ("мельников77", ("BMEL", "Борис Мельников")),
        # Имя латиницей при кириллическом имени в учётке (#967) и логин в ru-раскладке.
        ("Melnikov!2026", ("BMEL", "Борис Мельников")),
        ("Boris_1990!", ("BMEL", "Борис Мельников")),
        ("Yuliya-0512", ("YULK", "Юлия Ким")),
        ("ищкь-2026-x", ("BORM", "Борис Мельников")),
    ],
)
def test_password_with_login_or_name_rejected(password: str, personal: tuple[str, str]) -> None:
    assert _rejected(password, personal=personal) == ERR_AUTH_PASSWORD_GUESSABLE


@pytest.mark.parametrize(
    "password",
    [
        "correct-horse-battery",
        "new-horse-battery-staple",
        "Kx9!mQ2#vL",
        "лето в горах 2026",
        "Abcd!x7Kp",
        "синий чай в обед",
        "три зелёных яблока",
    ],
)
def test_reasonable_password_accepted(password: str) -> None:
    # Имя короче трёх знаков (`Ли`) и слово отеля короче четырёх внутри чужих слов не ищутся.
    ensure_password_policy(password, personal=("BORM", "Ли Борис"), hotel=(*HOTEL, "Spa"))


def test_short_password_reported_as_short_not_guessable() -> None:
    """Длина — первой: «1234567» отвечает про длину, а не про простоту."""
    assert _rejected("1234567") == ERR_AUTH_PASSWORD_TOO_SHORT


def test_huge_password_checked_in_linear_time() -> None:
    """Длину сверху ничто не ограничивает (тело формы до 1 МБ): проверка повтора
    и записей пароля обязана быть линейной, иначе один POST занимает процесс на минуты."""
    ensure_password_policy("x" * 500_000 + "y", personal=("BORM",), hotel=HOTEL)
    with pytest.raises(AppError):
        ensure_password_policy("ab" * 500_000, personal=(), hotel=())


async def test_hotel_words_from_tenant_and_config(canonical_database: None) -> None:
    """Название и код — из реестра отелей, город и страна — из конфига тенанта."""
    async with platform_session_scope() as session:
        tenant = Tenant(slug="samal-tau", name="Отель «Самал Тау»")
        session.add(tenant)
        await session.flush()
        await store_tenant_config(
            session,
            tenant.id,
            TenantConfig(
                profile=HotelProfile(city="Талдыкорган", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
            ),
        )

    assert await load_hotel_words("samal-tau") == HOTEL


async def test_hotel_words_without_config_or_tenant(canonical_database: None) -> None:
    """Онбординг не завершён — хватает названия и кода; отеля нет — пусто."""
    async with platform_session_scope() as session:
        session.add(Tenant(slug="no-config", name="Без конфига"))

    assert await load_hotel_words("no-config") == ("Без конфига", "no-config")
    assert await load_hotel_words("missing") == ()
