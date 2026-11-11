"""Политика пароля персонала: длина и «не угадывается первыми попытками» (#486, #967).

Выделено из `staff_credentials.py` (R-3: файл подходил к 400 строкам). Здесь —
только решение «годится ли пароль»; хэш и проверку пароля держит
`staff_credentials.hash_password`, который зовёт `ensure_password_policy` до argon2.

Лимит учётки — 10 отказов за 10 минут, то есть до 1440 попыток в сутки по логину,
который подсказывает открытый алгоритм (#402). Подбор начинают со списков частых
паролей, рядов клавиатуры и того, что видно о человеке и об отеле. Без zxcvbn-оценок
(P-1): отсекается только то, что угадывается первым:

- частый пароль — целиком или «словом в середине» (`common_passwords.txt`);
- ряд клавиатуры или алфавита, повтор одного куска;
- пароль без единой буквы — телефон, дата рождения (#967);
- логин или имя сотрудника, слова отеля и местности — название, код отеля, город,
  страна (#967), и несколько слов, общих для любого отеля, — в любой записи:
  кириллицей, латиницей по любой из ходовых транслитераций и в чужой раскладке
  (`Fkvfns` — «Алматы», набранное в латинской).

Транслитерации не перебираются: и слово, и пароль сводятся к одному «скелету» —
латинице, где ходовые варианты записи одного звука совпадают (`kh`/`h`/`х` → `h`,
`y`/`i`/`ы`/`й` → `i`, `q`/`қ`/`k` → `k`, `yu`/`iu`/`ю` → `u` и т. п.). Скелет
грубее слова, поэтому слово отеля короче четырёх знаков не проверяется.
"""

from __future__ import annotations

import re
import string
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Final

from sqlalchemy import select

from hospitality.platform.config import load_tenant_config
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AUTH_PASSWORD_TOO_SHORT = "ERR-AUTH-007"
ERR_AUTH_PASSWORD_GUESSABLE = "ERR-AUTH-016"

PASSWORD_MIN_LENGTH: Final = 8

# Частые пароли: источник, отбор и лицензия — в шапке файла.
_COMMON_PASSWORDS: Final = frozenset(
    line
    for line in Path(__file__)
    .with_name("common_passwords.txt")
    .read_text(encoding="utf-8")
    .splitlines()
    if line and not line.startswith("#")
)

# Ряды клавиатуры (латиница и йцукен) и алфавиты; кусок ряда в любую сторону —
# «12345678», «87654321», «qwertyui», «йцукенгш», «abcd1234».
_KEYBOARD_AND_ALPHABET_RUNS: Final = (
    "01234567890",
    "abcdefghijklmnopqrstuvwxyz",
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
    "йцукенгшщзхъ",
    "фывапролджэ",
    "ячсмитьбю",
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
)

# «Слово в середине» — пароль без цифр и знаков по краям: `Password1!` → `password`,
# `qwerty123` → `qwerty`. Короче четырёх знаков слово не проверяется.
_EDGE_CHARS: Final = string.digits + string.punctuation + " "
_MIN_WORD_LENGTH: Final = 4

# Часть логина или имени короче — слишком часто стоит внутри чужих слов.
_MIN_PERSONAL_PART_LENGTH: Final = 3
# Слово отеля сверяется и скелетом, а он грубее: трёхбуквенное «ата» или «spa»
# нашлось бы в половине парольных фраз.
_MIN_HOTEL_PART_LENGTH: Final = 4

# Слова любого отеля (#967): с них начнёт подбор тот, кто не знает о нём ничего.
# `password` уже в списке частых, `пароль` в нём только в en-раскладке (`gfhjkm`).
_GENERIC_WORDS: Final = ("пароль", "отель", "қонақүй", "necturn")

# Страна отеля (`HotelProfile.country_code`) — названия на языках, которыми её пишут;
# латиница выводится скелетом. Страны, где отелей нет, не нужны.
_COUNTRY_NAMES: Final[dict[str, tuple[str, ...]]] = {"KZ": ("казахстан", "қазақстан")}

# Кириллица ru и kk → латиница, по одной букве; варианты записи сводит `_LATIN_VARIANTS`.
# fmt: off
_CYRILLIC_TO_LATIN: Final = str.maketrans(
    {
        "а": "a", "ә": "a", "б": "b", "в": "v", "г": "g", "ғ": "g", "д": "d",
        "е": "e", "ё": "e", "ж": "j", "з": "z", "и": "i", "й": "i", "і": "i",
        "к": "k", "қ": "k", "л": "l", "м": "m", "н": "n", "ң": "n", "о": "o",
        "ө": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ұ": "u",
        "ү": "u", "ф": "f", "х": "h", "һ": "h", "ц": "c", "ч": "ch", "ш": "sh",
        "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "u", "я": "a",
    }
)
# fmt: on

# Ходовые латинские записи одного звука → одна. Порядок важен: длинные сочетания —
# раньше букв, из которых они состоят; `y` → `i` — раньше пар с `i`.
_LATIN_VARIANTS: Final = (
    ("shch", "sh"),
    ("sch", "sh"),
    ("kh", "h"),
    ("gh", "g"),
    ("zh", "j"),
    ("ts", "c"),
    ("x", "ks"),
    ("q", "k"),
    ("w", "v"),
    ("y", "i"),
)
_DOUBLE_I: Final = re.compile(r"i{2,}")
_IOTATED: Final = (("iu", "u"), ("ia", "a"), ("io", "o"), ("ie", "e"))

# Раскладки qwerty и йцукен, клавиша в клавишу (строчные — пароль уже casefold);
# с Shift латинская раскладка на тех же клавишах даёт знаки, а не заглавные буквы.
_LATIN_KEYS: Final = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_CYRILLIC_KEYS: Final = "йцукенгшщзхъфывапролджэячсмитьбюё"
_TYPED_IN_LATIN_LAYOUT: Final = str.maketrans(_LATIN_KEYS + '{}:"<>~', _CYRILLIC_KEYS + "хъжэбюё")
_TYPED_IN_CYRILLIC_LAYOUT: Final = str.maketrans(_CYRILLIC_KEYS, _LATIN_KEYS)


def _skeleton(folded: str) -> str:
    """Латинский скелет строки в нижнем регистре: кириллица → латиница, варианты
    транслитерации → одна запись. Всё линейно: длину пароля сверху ничто не держит."""
    text = folded.translate(_CYRILLIC_TO_LATIN)
    for variant, canonical in _LATIN_VARIANTS:
        text = text.replace(variant, canonical)
    text = _DOUBLE_I.sub("i", text)
    for variant, canonical in _IOTATED:
        text = text.replace(variant, canonical)
    return text


def _spellings(folded: str) -> tuple[str, ...]:
    """Пароль как есть, его скелет и скелеты набора в чужой раскладке."""
    return (
        folded,
        _skeleton(folded),
        _skeleton(folded.translate(_TYPED_IN_LATIN_LAYOUT)),
        _skeleton(folded.translate(_TYPED_IN_CYRILLIC_LAYOUT)),
    )


def _word_keys(values: Iterable[str], min_length: int) -> Iterator[str]:
    """Части значений (по небуквенным знакам) и их скелеты не короче `min_length`."""
    for value in values:
        for part in re.split(r"\W+", value.casefold()):
            if len(part) < min_length:
                continue
            yield part
            skeleton = _skeleton(part)
            if len(skeleton) >= min_length:
                yield skeleton


def _is_run(fragment: str) -> bool:
    return any(fragment in run or fragment in run[::-1] for run in _KEYBOARD_AND_ALPHABET_RUNS)


def _is_repetition(folded: str) -> bool:
    """Один кусок несколько раз: `BORMBORM`, `12121212`, `aaaaaaaa`. Строка —
    повтор куска ровно тогда, когда находится внутри себя удвоенной без краёв;
    поиск линейный, а длину пароля сверху ничто не ограничивает."""
    return folded in (folded + folded)[1:-1]


def _is_guessable(password: str, personal: Iterable[str], hotel: Iterable[str]) -> bool:
    folded = password.casefold()
    word = folded.strip(_EDGE_CHARS)
    fragments = (folded, word) if len(word) >= _MIN_WORD_LENGTH else (folded,)
    if (
        any(fragment in _COMMON_PASSWORDS or _is_run(fragment) for fragment in fragments)
        or _is_repetition(folded)
        or not any(char.isalpha() for char in password)
    ):
        return True
    keys = {
        *_word_keys(personal, _MIN_PERSONAL_PART_LENGTH),
        *_word_keys((*hotel, *_GENERIC_WORDS), _MIN_HOTEL_PART_LENGTH),
    }
    spellings = _spellings(folded)
    return any(key in spelling for key in keys for spelling in spellings)


def ensure_password_policy(password: str, *, personal: Iterable[str], hotel: Iterable[str]) -> None:
    """Единственный источник ERR-AUTH-007 и ERR-AUTH-016 (P-12).

    ERR-AUTH-007 — короче минимума; ERR-AUTH-016 — угадывается первыми попытками
    (перечень — в шапке модуля).

    `personal` — логин и имя того, кому задаётся пароль: оба видны коллегам и
    подсказываются алгоритмом логина (#402). `hotel` — слова отеля
    (`load_hotel_words`): название и код видны на той же странице, город и страна
    публичны. Оба аргумента обязательны, чтобы новая дверь не пропустила проверку
    молча.

    Зовётся ДО argon2 и до записи в БД: отказ ничего не меняет. Дверь по
    ссылке (приглашение, новый пароль) берёт логин и имя из той же ссылки, что
    уже показана её держателю, — недействительная ссылка отвечает своим отказом
    раньше политики при любом пароле, и отказ политики не говорит ничего сверх
    страницы и публичного об отеле (урок блокера ревью PR #159: исход не должен
    зависеть от того, что лежит в базе и не видно держателю).
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AppError(
            code=ERR_AUTH_PASSWORD_TOO_SHORT,
            message=f"Password must be at least {PASSWORD_MIN_LENGTH} characters long",
            status_code=422,
        )
    if _is_guessable(password, personal, hotel):
        logger.info("staff.password_rejected_guessable")
        raise AppError(
            code=ERR_AUTH_PASSWORD_GUESSABLE,
            message=(
                "Password is too easy to guess: no common passwords, keyboard runs, "
                "repeats, digits only, login, name, hotel or city"
            ),
            status_code=422,
        )


async def load_hotel_words(tenant_slug: str) -> tuple[str, ...]:
    """Слова отеля для `ensure_password_policy`: название, код, город, страна.

    По коду отеля — его знают все двери, где задаётся пароль (страница ссылки,
    команды сервера). Отеля с таким кодом нет — пусто: дверь откажет следом своим
    текстом. Конфиг не задан (онбординг) или не читается — хватает названия и кода:
    задать пароль это не мешает.
    """
    async with platform_session_scope() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
        if tenant is None:
            return ()
        try:
            profile = (await load_tenant_config(session, tenant.id)).profile
        except AppError:
            return (tenant.name, tenant.slug)
    return (
        tenant.name,
        tenant.slug,
        profile.city,
        *_COUNTRY_NAMES.get(profile.country_code, ()),
    )
