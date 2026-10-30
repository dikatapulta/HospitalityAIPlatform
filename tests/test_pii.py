"""Маскирование платёжных паттернов (spec 0031, NG-3, issue #128).

Чистые юниты помощника `shared/pii.py`: что маскируется (целые группы на 13–19
цифр + Луна, с пробелами/дефисами/точками, при любых соседях — issue #239), что
сознательно не трогается (телефоны, ряды без Луна, приклеенное без разделителя)
— план тестов спеки, п. 1.
"""

from __future__ import annotations

import random
import re

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
    ("text", "masked"),
    [
        # Строки issue #239: соседняя цифра через разделитель прятала весь ряд.
        ("карта 4111 1111 1111 1111 5", "карта [card ****1111] 5"),
        ("карта 4111 1111 1111 1111 03", "карта [card ****1111] 03"),
        ("карта 4111 1111 1111 1111 0327", "карта [card ****1111] 0327"),
        ("карта 4111-1111-1111-1111-5", "карта [card ****1111]-5"),
        ("мой номер 8 4111 1111 1111 1111", "мой номер 8 [card ****1111]"),
        ("/done 4111111111111111 1", "/done [card ****1111] 1"),
        # Номер заявки или комнаты перед картой — тот же класс.
        ("/done #12 4111 1111 1111 1111 0327", "/done #12 [card ****1111] 0327"),
        ("комната 704 4111 1111 1111 1111", "комната 704 [card ****1111]"),
        ("4111 1111 1111 1111 1111", "[card ****1111] 1111"),
        # Точка — третий разделитель (пятый проход ревью PR #202).
        ("4111.1111.1111.1111", "[card ****1111]"),
        ("3782 822463 10005 03 27", "[card ****0005] 03 27"),
    ],
)
def test_card_with_neighbouring_digits_is_masked(text: str, masked: str) -> None:
    assert mask_payment_card_numbers(text) == masked


def test_two_cards_in_one_run_stay_two_marks() -> None:
    text = f"{VISA_16} {MASTERCARD_16}"
    assert mask_payment_card_numbers(text) == "[card ****1111] [card ****4444]"


@pytest.mark.parametrize(
    "text",
    [
        "87012345678 87023456789",  # два телефона: ни одного отрезка на 13–19 цифр
        "4111 1111 1111 1112 2",  # отрезки есть, Луна не сходится ни на одном
    ],
)
def test_runs_without_luhn_candidate_untouched(text: str) -> None:
    assert mask_payment_card_numbers(text) == text


def test_card_glued_without_separator_is_accepted_gap() -> None:
    """Приклеенное без разделителя не ищется (spec 0031 §1): край карты — край группы.

    Поиск на любой цифре задевал бы 50–99 % длинных чисел — списки номеров
    комнат, IBAN, два телефона подряд; остаток назван в спеке.
    """
    assert mask_payment_card_numbers("41111111111111110327") == "41111111111111110327"


def test_every_luhn_candidate_is_closed_whatever_stands_next() -> None:
    """Карта, набранная слитно или группами, закрывается при любых соседях.

    Соседи — случайные группы через случайный разделитель: каждый случайный
    отрезок, прошедший Луна, сливается с картой, и ни одна её цифра не остаётся
    снаружи. Повторный прогон канона ничего не меняет.
    """
    rng = random.Random(239)
    pans = [VISA_16, MASTERCARD_16, AMEX_15, VISA_13, MAESTRO_19]
    for _ in range(3000):
        pan = rng.choice(pans)
        typed = rng.choice([pan, " ".join(pan[i : i + 4] for i in range(0, len(pan), 4))])
        left = [_random_group(rng) for _ in range(rng.randint(0, 4))]
        right = [_random_group(rng) for _ in range(rng.randint(0, 4))]
        run = _join_random(rng, [*left, typed, *right])
        masked = mask_payment_card_numbers(f"оплата {run} спасибо")
        assert pan not in re.sub(r"\D", "", masked), run
        assert mask_payment_card_numbers(masked) == masked, run


def _random_group(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789") for _ in range(rng.randint(1, 6)))


def _join_random(rng: random.Random, groups: list[str]) -> str:
    joined = groups[0]
    for group in groups[1:]:
        joined += rng.choice(" .-") + group
    return joined


def test_text_without_digits_unchanged() -> None:
    text = "уберите номер, пожалуйста"
    assert mask_payment_card_numbers(text) == text
