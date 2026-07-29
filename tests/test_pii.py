"""Маскирование платёжных паттернов (spec 0031, NG-3, issue #128).

Чистые юниты помощника `shared/pii.py`: что маскируется (13–19 цифр + Луна,
с пробелами/дефисами), что сознательно не трогается (телефоны, ряды без Луна,
20+ цифр) — план тестов спеки, п. 1.
"""

from __future__ import annotations

import pytest

from hospitality.shared.pii import mask_payment_card_numbers

# Публичные тестовые PAN платёжных систем — все проходят Луна.
VISA_16 = "4111111111111111"
MASTERCARD_16 = "5555555555554444"
AMEX_15 = "378282246310005"
VISA_13 = "4222222222222"
MAESTRO_19 = "6799990100000000019"


@pytest.mark.parametrize(
    "pan",
    [VISA_16, MASTERCARD_16, AMEX_15, VISA_13, MAESTRO_19],
)
def test_valid_pan_is_masked_keeping_last_four(pan: str) -> None:
    assert mask_payment_card_numbers(pan) == f"[card ****{pan[-4:]}]"


@pytest.mark.parametrize(
    "formatted",
    [
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "3782 822463 10005",
    ],
)
def test_pan_with_separators_is_masked(formatted: str) -> None:
    last_four = formatted.replace(" ", "").replace("-", "")[-4:]
    assert mask_payment_card_numbers(formatted) == f"[card ****{last_four}]"


def test_pan_inside_sentence_keeps_surrounding_text() -> None:
    text = "оплатите с карты 4111 1111 1111 1111, пожалуйста"
    assert mask_payment_card_numbers(text) == "оплатите с карты [card ****1111], пожалуйста"


def test_multiple_cards_in_one_text() -> None:
    text = f"старая {VISA_16}, новая {MASTERCARD_16}"
    assert mask_payment_card_numbers(text) == "старая [card ****1111], новая [card ****4444]"


def test_sixteen_digits_failing_luhn_untouched() -> None:
    # Трек-номера и прочие случайные ряды не проходят контрольную сумму.
    assert mask_payment_card_numbers("4111111111111112") == "4111111111111112"


def test_phone_numbers_untouched() -> None:
    # Телефоны короче 13 цифр — не кандидаты, Луна даже не проверяется.
    text = "позвоните +7 707 123 45 67 или 87071234567"
    assert mask_payment_card_numbers(text) == text


@pytest.mark.parametrize(
    "long_run",
    [
        "41111111111111111111",  # 20 цифр слитно — не PAN
        "4111 1111 1111 1111 1111",  # 20 цифр с пробелами — подряд не маскируем
    ],
)
def test_longer_digit_runs_untouched(long_run: str) -> None:
    # Подпоследовательность длинного ряда маскировать нельзя: это не карта.
    assert mask_payment_card_numbers(long_run) == long_run


def test_text_without_digits_unchanged() -> None:
    text = "уберите номер, пожалуйста"
    assert mask_payment_card_numbers(text) == text
