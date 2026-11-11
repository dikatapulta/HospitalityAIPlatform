"""Алфавит текста — латиница, кириллица или иной (#1043, spec 0035 §8.2).

Один источник проверки «на каком алфавите написан текст» для наборов evals и счётчика
языка ответа в ходе гостя. Без библиотек: буква относится к алфавиту по имени
символа Unicode (`LATIN …`, `CYRILLIC …`), поэтому казахские `ә`, `қ`, `ү`, русская
`ё` и турецкие `ş`, `ğ` попадают в свой алфавит. Русский и казахский — оба
кириллица, английский и турецкий — оба латиница: язык внутри алфавита этот модуль
не различает (определитель языка — позже, #1043 п. 2).
"""

from __future__ import annotations

import enum
import unicodedata
from collections import Counter


class Alphabet(enum.StrEnum):
    """Алфавит буквы; `OTHER` — буква вне латиницы и кириллицы (китайский, арабский, хинди)."""

    LATIN = "latin"
    CYRILLIC = "cyrillic"
    OTHER = "other"


def letter_alphabet(char: str) -> Alphabet | None:
    """Алфавит одного символа; не буква (цифра, знак, эмодзи) — None."""
    if not char.isalpha():
        return None
    name = unicodedata.name(char, "")
    if name.startswith("LATIN"):
        return Alphabet.LATIN
    if name.startswith("CYRILLIC"):
        return Alphabet.CYRILLIC
    return Alphabet.OTHER


def has_cyrillic(text: str) -> bool:
    """Есть ли в тексте хоть одна кириллическая буква — строгая проверка наборов evals."""
    return any(letter_alphabet(char) is Alphabet.CYRILLIC for char in text)


def dominant_alphabet(text: str, *, min_letters: int, min_share: float) -> Alphabet | None:
    """Алфавит, которым написана доля букв не меньше `min_share`; иначе None.

    None — и когда букв меньше `min_letters`: по «ok» или «101» алфавит гостя не
    узнать. Смесь без явного большинства (трёхъязычный статический текст: казахский
    и русский — кириллица, английский — латиница) тоже None — такой текст ни на
    каком одном алфавите не написан.
    """
    counts = Counter(
        alphabet for alphabet in (letter_alphabet(char) for char in text) if alphabet is not None
    )
    total = sum(counts.values())
    if total < min_letters:
        return None
    alphabet, count = counts.most_common(1)[0]
    return alphabet if count >= total * min_share else None
