"""Строка согласия под формами 3D-витрины (spec 0069 §2.6, §5; PR (в), #805).

Отправка формы и есть согласие: строка стоит под кнопкой «Отправить запрос», в
квадратных скобках — ссылка на политику витрины (`/legal/showroom-privacy`).
Версия и время согласия ложатся в строку запроса (`showroom_requests`).

Тексты дословно совпадают с преамбулой `docs/legal/showroom-privacy.md` — сверяет
`tests/test_legal.py`. **Изменил текст строки или политики по смыслу → подними
`SHOWROOM_CONSENT_VERSION`.** Строка — «да» основателя 04.11 для staging и показа
как есть; перед живыми посетителями — решение окна о строке и политике по правилу #947
(тело #805).
"""

from __future__ import annotations

import html
from typing import Final

from hospitality.platform.legal import SHOWROOM_PRIVACY_PATH

SHOWROOM_CONSENT_VERSION: Final = "2026-11-04-v1"

CONSENT_TEXTS: Final[dict[str, str]] = {
    "en": (
        "By sending this request, I agree to the hotel and the platform processing the data "
        "I entered in order to answer this request — [showroom privacy policy]."
    ),
    "ru": (
        "Отправляя запрос, я соглашаюсь на обработку указанных данных отелем и платформой "
        "для ответа на этот запрос — [политика конфиденциальности витрины]."
    ),
    "kk": (
        "Сұрауды жіберу арқылы мен көрсетілген деректерді қонақүй мен платформаның осы "
        "сұрауға жауап беру үшін өңдеуіне келісемін — [витринаның құпиялылық саясаты]."
    ),
}


def consent_html(language: str) -> str:
    """Строка согласия на языке страницы; ссылка — на политику витрины, в новой вкладке."""
    text = CONSENT_TEXTS.get(language, CONSENT_TEXTS["en"])
    before, _, rest = text.partition("[")
    label, _, after = rest.partition("]")
    return (
        f'{html.escape(before)}<a href="{SHOWROOM_PRIVACY_PATH}" target="_blank" '
        f'rel="noopener">{html.escape(label)}</a>{html.escape(after)}'
    )
