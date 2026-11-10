"""Скрипты страниц меню и документов зовут только существующие надписи (#755).

Без БД: страницы — статический HTML, собранный при импорте. Ключ, которого нет в
`texts.py`, скрипт покажет гостю как есть («err_load» вместо фразы), и ни один
другой тест этого не заметит.
"""

from __future__ import annotations

import re

import pytest

from hospitality.channels.web.documents import page as documents_page
from hospitality.channels.web.documents import texts as documents_texts
from hospitality.channels.web.menu import page as menu_page
from hospitality.channels.web.menu import texts as menu_texts

# Литеральный ключ: `t("sold_out")`, `t("room", {…})` и ошибки из таблиц кодов (`"err_closed"`);
# ключи, собранные на лету (`t("stage_" + s)`), сюда не попадают.
_T = re.compile(r'\bt\("([a-z_]+)"[,)]')
_TC = re.compile(r'\btc\("([a-z_]+)"[,)]')
_ERR = re.compile(r'"(err_[a-z_]+)"')


def test_menu_fragments_are_pasted() -> None:
    assert "__NAV_JS__" not in menu_page.JS and "__SHEET_JS__" not in menu_page.JS
    assert "function leave(" in menu_page.JS and "function openSheet(" in menu_page.JS


def test_menu_script_keys_exist() -> None:
    keys = set(_T.findall(menu_page.JS)) | set(_ERR.findall(menu_page.JS))
    keys |= {
        f"{key}_{catalog}"
        for key in _TC.findall(menu_page.JS)
        for catalog in ("restaurant", "shop")
    }
    assert {"err_load", "err_offline", "cleared", "undo", "close", "retry"} <= keys
    assert keys - set(menu_texts.PAGE_TEXTS) == set()


def test_documents_script_keys_exist() -> None:
    keys = set(_T.findall(documents_page.JS)) | set(_ERR.findall(documents_page.JS))
    assert {"err_load", "retry", "disabled_note"} <= keys
    assert keys - set(documents_texts.PAGE_TEXTS) == set()


@pytest.mark.parametrize(
    ("texts", "key"),
    [
        (menu_texts.PAGE_TEXTS, "sold_out_note_restaurant"),
        (menu_texts.PAGE_TEXTS, "sold_out_note_shop"),
        (menu_texts.PAGE_TEXTS, "err_offline"),
        (documents_texts.PAGE_TEXTS, "err_load"),
        (documents_texts.PAGE_TEXTS, "disabled_note"),
    ],
)
def test_new_texts_have_three_languages(texts: dict[str, tuple[str, str, str]], key: str) -> None:
    assert all(text.strip() for text in texts[key])
