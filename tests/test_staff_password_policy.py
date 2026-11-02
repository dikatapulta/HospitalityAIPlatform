"""Политика пароля персонала (issue #486): длина и «не угадывается первыми попытками».

Чистая функция `ensure_password_policy` — без БД; двери (приглашение, ссылка
нового пароля) проверяются страничными тестами `staff_portal/tests`, бутстрап —
`test_staff_bootstrap_tool.py`.
"""

from __future__ import annotations

import pytest

from hospitality.platform.staff_credentials import (
    ERR_AUTH_PASSWORD_GUESSABLE,
    ERR_AUTH_PASSWORD_TOO_SHORT,
    ensure_password_policy,
)
from hospitality.shared.errors import AppError


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
    with pytest.raises(AppError) as error:
        ensure_password_policy(password, personal=())
    assert error.value.code == ERR_AUTH_PASSWORD_GUESSABLE
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    ("password", "personal"),
    [
        ("borm-hotel-7", ("BORM", "Борис Мельников")),
        ("Melnikov!2026", ("BMEL", "Boris Melnikov")),
        ("мельников77", ("BMEL", "Борис Мельников")),
    ],
)
def test_password_with_login_or_name_rejected(password: str, personal: tuple[str, str]) -> None:
    with pytest.raises(AppError) as error:
        ensure_password_policy(password, personal=personal)
    assert error.value.code == ERR_AUTH_PASSWORD_GUESSABLE


@pytest.mark.parametrize(
    "password",
    [
        "correct-horse-battery",
        "new-horse-battery-staple",
        "Kx9!mQ2#vL",
        "лето в горах 2026",
        "Abcd!x7Kp",
    ],
)
def test_reasonable_password_accepted(password: str) -> None:
    # Имя короче трёх знаков (`Ли`) внутри чужих слов не ищется.
    ensure_password_policy(password, personal=("BORM", "Ли Борис"))


def test_short_password_reported_as_short_not_guessable() -> None:
    """Длина — первой: «1234567» отвечает про длину, а не про простоту."""
    with pytest.raises(AppError) as error:
        ensure_password_policy("1234567", personal=())
    assert error.value.code == ERR_AUTH_PASSWORD_TOO_SHORT


def test_huge_password_checked_in_linear_time() -> None:
    """Длину сверху ничто не ограничивает (тело формы до 1 МБ): проверка повтора
    обязана быть линейной, иначе один POST занимает процесс на минуты."""
    ensure_password_policy("x" * 500_000 + "y", personal=("BORM",))
    with pytest.raises(AppError):
        ensure_password_policy("ab" * 500_000, personal=())
