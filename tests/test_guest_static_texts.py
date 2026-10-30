"""Статические тексты гостя — на казахском, русском и английском (issue #224).

Решение основателя 06.08 (аудит §12 п. 6): казахский во всех текстах, которые
гость видит без модели. Язык гостя там неизвестен, поэтому текст — три строки
в порядке канона согласия (`channels/common/consent.py`): kk, ru, en. Тест
держит обе половины: новый текст без казахского и строка не на своём месте
падают здесь, а не на глазах у гостя.
"""

from __future__ import annotations

import re

import pytest

from hospitality.ai import orchestrator
from hospitality.ai.tools import cancel_service_request, create_service_request, escalate_to_staff
from hospitality.channels.common import chat_limits, guest_turn
from hospitality.channels.web import page, service

# Буквы, которых нет в русском алфавите: по ним строку узнаёт как казахскую.
_KK_LETTERS = re.compile("[әғқңөұүһіӘҒҚҢӨҰҮҺІ]")
_CYRILLIC = re.compile("[а-яёА-ЯЁ]")

TEXTS: dict[str, str] = {
    "UNSUPPORTED_REPLY": guest_turn.UNSUPPORTED_REPLY,
    "DEGRADED_REPLY": guest_turn.DEGRADED_REPLY,
    "no_staff_reply с цифрой": guest_turn.no_staff_reply(None, "0"),
    "no_staff_reply без цифры": guest_turn.no_staff_reply(None, None),
    "RATE_LIMITED_REPLY": chat_limits.RATE_LIMITED_REPLY,
    "DAILY_LIMIT_REPLY": chat_limits.DAILY_LIMIT_REPLY,
    "MESSAGE_TOO_LONG_REPLY": chat_limits.MESSAGE_TOO_LONG_REPLY,
    "web: нет сессии": service._UNAUTHENTICATED_TEXT,
    "web: код не подошёл": service._CODE_REJECTED_TEXT,
    "web: много попыток": service._CODE_RATE_LIMITED_TEXT,
    "web: QR погашен": service._BINDLINK_EXPIRED_TEXT,
    "резерв: эскалация": orchestrator._ESCALATION_TEXT,
    "резерв: отказ": orchestrator._DECLINED_TEXT,
    "резерв: факта нет": orchestrator._UNANSWERED_TEXT,
    "резерв: заявка создана": create_service_request.DONE_TEXT,
    "резерв: заявка отменена": cancel_service_request.DONE_TEXT,
    "резерв: сотрудник позван": escalate_to_staff.FALLBACK_REPLY,
}


@pytest.mark.parametrize("text", TEXTS.values(), ids=TEXTS.keys())
def test_static_guest_text_is_kazakh_russian_english(text: str) -> None:
    """Три строки: казахская, русская, английская — именно в этом порядке."""
    lines = text.split("\n")
    assert len(lines) == 3, lines
    kk, ru, en = lines
    assert _KK_LETTERS.search(kk), kk
    assert _CYRILLIC.search(ru) and not _KK_LETTERS.search(ru), ru
    assert not _CYRILLIC.search(en), en


def test_no_staff_reply_phone_line_follows_the_three_languages() -> None:
    """Строка телефона — четвёртой, после трёх языков (spec 0051 §3)."""
    lines = guest_turn.no_staff_reply("+7 727 000 00 00", "0").split("\n")
    assert len(lines) == 4
    assert lines[3] == "📞 Ресепшен / Reception: +7 727 000 00 00"


@pytest.mark.parametrize(
    "kazakh",
    [
        "Қонақүй чаты",
        "Бөлме · Комната · Room",
        "Хабарлама · Сообщение · Message",
        "Қосылдыңыз. Қалай көмектесе аламыз?",
        "Жеткізілмеді, қайталап көріңіз",
        "Қате, қайталап көріңіз",
        "Келісім · Согласие · Consent",
    ],
)
def test_chat_page_labels_carry_kazakh(kazakh: str) -> None:
    """Надписи страницы веб-чата — и на казахском (#224)."""
    assert kazakh in page.render()


def test_bind_page_error_carries_kazakh() -> None:
    """Страница ссылки с листка: заголовок, запасная ошибка и подпись согласия — с казахским."""
    html = page.render_bind()
    assert "Қонақүй чаты" in html
    assert "Қате, қайталап көріңіз" in html
    assert "Келісім · Согласие · Consent" in html
