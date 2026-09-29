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
        # Вторая половина каждой правки списка по ревью PR #291: щит от обычного
        # употребления не имеет права съесть само ЧП.
        ("у меня в номере горит проводка", EmergencyKind.FIRE, "ru"),
        # Место пожара называют предложным падежом, и слово может кончаться
        # на «-не»: щит отрицания обязан требовать границы слова, иначе
        # «в стене» гасит маркер так же, как частица «не» (ревью PR #291, Н2-1).
        ("в стене горит проводка", EmergencyKind.FIRE, "ru"),
        ("загорелась шторка в ванной", EmergencyKind.FIRE, "ru"),
        ("there is smoke in my room", EmergencyKind.FIRE, "en"),
        ("I smell smoke in the corridor", EmergencyKind.FIRE, "en"),
        ("please call a doctor, my wife is unwell", EmergencyKind.MEDICAL, "en"),
        ("маған дәрігер керек", EmergencyKind.MEDICAL, "kk"),
        ("дәрігер шақырыңыз", EmergencyKind.MEDICAL, "kk"),
        ("мені қорқытып жатыр", EmergencyKind.SECURITY, "kk"),
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
        # Отрицание — не пожар (ревью PR #291, Н-1): два самых частых обычных
        # сообщения отеля до этой правки перехватывались как ЧП, и заявки на
        # перегоревшую лампочку не создавалось вовсе.
        "в ванной не горит свет",
        "свет не горит, поменяйте лампочку",
        "не горит телевизор в номере",
        # Вопрос о правилах отеля — тоже не пожар (там же).
        "Can I smoke in the room?",
        "where can i smoke?",
        "may we smoke on the balcony?",
        "I want to smoke, is there a place?",
        # Слово с обычным употреблением берётся только в связке — правило §1
        # спеки, применённое ко всем трём языкам (ревью PR #291, Н-2).
        "есть ли в отеле врач?",
        "is there a doctor in the hotel?",
        "do you have a doctor?",
        "қонақүйде дәрігер бар ма?",
        # Имя собственное: аэропорт Кызылорды «Қорқыт Ата» (там же).
        "Қорқыт Ата әуежайына қалай жетуге болады?",
        # «Загорел» — про пляж, а не про проводку (там же).
        "я загорел, есть ли у вас крем после загара?",
        "",
    ],
)
def test_ordinary_message_is_not_an_emergency(text: str) -> None:
    assert urgency.detect_emergency(text) is None


def test_emergency_reply_sends_guest_to_reception_with_dial_and_phone() -> None:
    """Текст перехвата — отправить гостя на ресепшен (решение основателя 29.09, #382).

    Что набрать из номера — из конфига отеля (`reception_room_dial`), мобильный
    ресепшена — строкой ниже (`reception_phone`). Без номеров экстренных служб
    (решение основателя 19.08, spec 0034 §3).
    """
    reply = urgency.emergency_reply("ru", "+7 727 000 00 00", reception_room_dial="0")
    assert reply == (
        "Позвоните на ресепшен прямо сейчас — наберите 0 с телефона в номере "
        "или подойдите к стойке. Там помогут сразу.\n"
        "📞 Ресепшен: +7 727 000 00 00"
    )


def test_emergency_reply_without_room_dial_names_no_digit() -> None:
    """Цифра не задана — фраза без неё, а не «наберите  с телефона»."""
    reply = urgency.emergency_reply("ru", "+7 727 000 00 00")
    assert reply == (
        "Позвоните на ресепшен прямо сейчас с телефона в номере "
        "или подойдите к стойке. Там помогут сразу.\n"
        "📞 Ресепшен: +7 727 000 00 00"
    )


@pytest.mark.parametrize(
    ("language", "staff_word"), [("ru", "персонал"), ("kk", "қызметкер"), ("en", "staff")]
)
def test_emergency_reply_does_not_promise_staff(language: str, staff_word: str) -> None:
    """Гостю не пишется «передаю персоналу» (решение основателя 29.09, #382).

    Сигнал в staff-чат уходит, как и раньше, но текст гостю отправляет его на
    ресепшен и ничего не обещает от имени персонала. Щит от возврата первого
    абзаца «по инерции»: он остаётся у срочной заявки, где персонал заявку
    действительно получил. Слово «персонал» проверяется отдельно: обещание
    другими словами («Сообщил персоналу — позвоните на ресепшен») точную фразу
    срочной заявки не содержит (ревью PR #383).
    """
    for dial in (None, "0"):
        reply = urgency.emergency_reply(language, None, reception_room_dial=dial)
        assert urgency.urgent_accepted_reply(language) not in reply
        assert staff_word not in reply.casefold()


def test_no_emergency_service_numbers_in_any_language() -> None:
    """Ни 112, ни 101/102/103 ни на одном языке (решение основателя 19.08).

    Щит от возврата номера «по инерции» в следующей правке текстов: номер
    экстренной службы в ответе бота — это указание гостю сделать то, что обязан
    сделать отель.
    """
    for language in ("ru", "kk", "en"):
        for phone in (None, "+7 727 000 00 00"):
            for dial in (None, "0"):
                reply = urgency.emergency_reply(language, phone, reception_room_dial=dial)
                assert not any(number in reply for number in ("112", "101", "102", "103"))


def test_emergency_reply_without_reception_contacts_stays_actionable() -> None:
    """Телефон и цифра не настроены — строки с номером нет, текст действует.

    «📞 Ресепшен: —» это не контакт: гость в дыму не должен читать прочерк.
    «Позвоните с телефона в номере или подойдите к стойке» от настроек не зависит.
    """
    reply = urgency.emergency_reply("ru", None)
    assert "📞" not in reply
    assert "Позвоните на ресепшен" in reply


@pytest.mark.parametrize("language", ["ru", "kk", "en"])
def test_texts_exist_for_every_supported_language(language: str) -> None:
    """Три языка статических текстов гостя (решение основателя, аудит §12 п. 6)."""
    assert urgency.urgent_accepted_reply(language)
    assert urgency.emergency_reply(language, None)
    assert "0" in urgency.emergency_reply(language, None, reception_room_dial="0")


@pytest.mark.parametrize("language", [None, "zz", "de", ""])
def test_unknown_language_degrades_to_russian(language: str | None) -> None:
    assert urgency.urgent_accepted_reply(language) == urgency.urgent_accepted_reply("ru")
    assert urgency.emergency_reply(language, None) == urgency.emergency_reply("ru", None)
    assert urgency.emergency_reply(
        language, None, reception_room_dial="0"
    ) == urgency.emergency_reply("ru", None, reception_room_dial="0")
