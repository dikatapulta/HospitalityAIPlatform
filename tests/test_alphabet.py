"""Алфавит текста — `shared/alphabet.py` (#1043, spec 0035 §8.2)."""

from __future__ import annotations

import pytest

from hospitality.shared.alphabet import (
    Alphabet,
    dominant_alphabet,
    has_cyrillic,
    letter_alphabet,
)


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ("a", Alphabet.LATIN),
        ("ş", Alphabet.LATIN),  # турецкий
        ("я", Alphabet.CYRILLIC),
        ("ё", Alphabet.CYRILLIC),
        ("ә", Alphabet.CYRILLIC),  # казахский
        ("Қ", Alphabet.CYRILLIC),
        ("早", Alphabet.OTHER),
        ("न", Alphabet.OTHER),  # хинди
        ("7", None),
        ("?", None),
        ("🙂", None),
    ],
)
def test_letter_alphabet(char: str, expected: Alphabet | None) -> None:
    assert letter_alphabet(char) is expected


def test_has_cyrillic_sees_letters_outside_the_basic_range() -> None:
    # Прежняя проверка наборов `"а" <= c <= "я"` пропускала «ё» и казахские буквы.
    assert has_cyrillic("Hello, ёж")
    assert has_cyrillic("Сәлем")
    assert has_cyrillic("Ә")
    assert not has_cyrillic("Hello, world! 305 ✓")


def test_dominant_alphabet_needs_letters_and_a_clear_majority() -> None:
    assert dominant_alphabet("Hello there", min_letters=10, min_share=0.8) is Alphabet.LATIN
    assert dominant_alphabet("Hello", min_letters=10, min_share=0.8) is None
    # 8 латинских из 10 — ровно доля 0.8.
    assert dominant_alphabet("abcdefghЖЖ", min_letters=10, min_share=0.8) is Alphabet.LATIN
    assert dominant_alphabet("abcdefgЖЖЖ", min_letters=10, min_share=0.8) is None
