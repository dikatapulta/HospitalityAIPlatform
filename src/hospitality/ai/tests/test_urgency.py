"""ЧП-детектор и статические тексты срочности (spec 0034, issue #208, R-7).

Юнит-тесты чистого модуля: ни БД, ни LLM, ни канала. Сквозной путь «сообщение
гостя → эскалация персоналу → текст с телефонами» — в
`channels/telegram/tests/test_emergency.py`.
"""

from __future__ import annotations

import pytest

from hospitality.ai import urgency
from hospitality.ai.urgency import EmergencyKind


@pytest.mark.parametrize(
    ("text", "kind", "language"),
    [
        ("В номере дым, что делать?", EmergencyKind.FIRE, "ru"),
        ("ПОЖАР!!!", EmergencyKind.FIRE, "ru"),
        ("Бөлмеде өрт шықты", EmergencyKind.FIRE, "kk"),
        ("There is a fire on the 3rd floor", EmergencyKind.FIRE, "en"),
        ("Вызовите скорую, пожалуйста", EmergencyKind.MEDICAL, "ru"),
        ("Жедел жәрдем шақырыңыз", EmergencyKind.MEDICAL, "kk"),
        ("we need an ambulance", EmergencyKind.MEDICAL, "en"),
        ("На меня напали в коридоре", EmergencyKind.SECURITY, "ru"),
        ("someone attacked me", EmergencyKind.SECURITY, "en"),
    ],
)
def test_detects_emergency_kind_and_language(text: str, kind: EmergencyKind, language: str) -> None:
    """Вид ЧП — для логов, язык маркера — для ответа гостю (spec 0034 §1, §2)."""
    match = urgency.detect_emergency(text)
    assert match is not None
    assert match.kind is kind
    assert match.language == language


@pytest.mark.parametrize(
    "text",
    [
        "принесите, пожалуйста, полотенца в 305",
        # Слово целиком, а не подстрока: вопрос про эвакуацию — обычный вопрос
        # гостя, а не ЧП (spec 0034 §1).
        "где находится пожарный выход?",
        "Where is the emergency exit?",
        "в номере не работает пожарная сигнализация",
        # Общие слова помощи в список не входят намеренно: в чате отеля это
        # чаще всего «помогите с телевизором».
        "помогите разобраться с телевизором",
        "please help me with the wifi",
        "",
    ],
)
def test_ordinary_message_is_not_an_emergency(text: str) -> None:
    assert urgency.detect_emergency(text) is None


def test_emergency_reply_contains_approved_text_and_both_phones() -> None:
    """Текст утверждён основателем дословно (аудит §12 п. 5, spec 0034 §3)."""
    reply = urgency.emergency_reply("ru", "+7 727 000 00 00")
    assert reply == (
        "Понял, это срочно. Уже передаю персоналу отеля.\n"
        "\n"
        "Если есть угроза жизни или здоровью — не ждите ответа в чате:\n"
        "📞 Ресепшен: +7 727 000 00 00\n"
        "📞 112 — служба спасения"
    )


def test_emergency_reply_without_reception_phone_keeps_emergency_number() -> None:
    """Телефон отеля не настроен — строки ресепшена нет, 112 остаётся.

    «📞 Ресепшен: —» это не контакт: гость в дыму не должен читать прочерк.
    """
    reply = urgency.emergency_reply("ru", None)
    assert "Ресепшен" not in reply
    assert "📞 112 — служба спасения" in reply


@pytest.mark.parametrize("language", ["ru", "kk", "en"])
def test_texts_exist_for_every_supported_language(language: str) -> None:
    """Три языка статических текстов гостя (решение основателя, аудит §12 п. 6)."""
    assert urgency.urgent_accepted_reply(language)
    assert urgency.EMERGENCY_NUMBER in urgency.emergency_reply(language, None)
    # Первый абзац общий у двух путей: перехвата и срочной заявки (spec 0034 §3).
    assert urgency.emergency_reply(language, None).startswith(
        urgency.urgent_accepted_reply(language)
    )


@pytest.mark.parametrize("language", [None, "zz", "de", ""])
def test_unknown_language_degrades_to_russian(language: str | None) -> None:
    assert urgency.urgent_accepted_reply(language) == urgency.urgent_accepted_reply("ru")
    assert urgency.emergency_reply(language, None) == urgency.emergency_reply("ru", None)
