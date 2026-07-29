"""Согласие гостя на обработку ПД — канон обоих гостевых каналов (spec 0029).

CANONICAL: единственное место, где живут версия согласия, его тексты и правило
«согласие действительно». Telegram и web копируют отсюда, а не пишут своё:
разъехавшиеся тексты согласия — юридический дефект, а не косметика.

Тексты — ДОСЛОВНАЯ копия `docs/legal/consent-text.md` (он источник истины,
`docs/legal/README.md`); расхождение ловит тест `tests/test_legal.py`. Копия, а
не чтение файла в рантайме (в отличие от страницы политики,
`platform/legal.py`): сломанный разбор документа оставил бы гостя без
возможности дать согласие, то есть убил бы канал целиком.

**Изменил текст по смыслу → подними `CONSENT_VERSION`** здесь и в
`docs/legal/consent-text.md` тем же PR: гости с прежней версией проходят гейт
заново (spec 0029 §6).
"""

from __future__ import annotations

from hospitality.platform.legal import privacy_policy_url

# Версия текста согласия (docs/legal/consent-text.md, «Версия»). Пишется рядом
# с фактом согласия: `conversations.consent_version` (telegram, spec 0029 §1) и
# `guest_sessions.consent_version` (web, spec 0027). Формат — дата-vN; колонки
# VARCHAR(16), длиннее не влезет.
CONSENT_VERSION = "2026-07-28-v1"

# Языки согласия и порядок показа при неизвестном языке гостя: kk+ru —
# законодательный минимум (Закон «О языках», ЗоЗПП), en — демография пилота.
CONSENT_LANGUAGES = ("kk", "ru", "en")

# Плейсхолдер ссылки на политику в каждом языке (docs/legal/consent-text.md):
# тексты хранятся дословно, URL подставляется при показе — так дословность
# копии проверяема тестом, а адрес остаётся настройкой окружения.
_POLICY_PLACEHOLDERS = {
    "kk": "[саясат сілтемесі]",
    "ru": "[ссылка на политику]",
    "en": "[privacy policy URL]",
}

_TEXTS = {
    "kk": (
        "Мен дербес деректерімді — хабарлама мәтіндерін, чат идентификаторын, "
        "бөлме нөмірі мен қонақүйде тұру кезеңін — тұруыма қызмет көрсету "
        "мақсатында өңдеуге келісемін.\n\n"
        "Жауап дайындау үшін хабарлама мәтіндері Қазақстаннан тыс жердегі өңдеу "
        "сервисіне (Anthropic, АҚШ — «Дербес деректер және оларды қорғау туралы» "
        "ҚР Заңының 16-бабы мағынасында дербес деректердің қорғалуын қамтамасыз "
        "етпейтін ел) берілетінімен келісемін.\n\n"
        "Жауаптарды жасанды интеллект дайындайды. Сіз автоматтандырылған өңдеуге "
        "қарсылық білдіруге және қызметкермен сөйлесуге құқылысыз (Заңның "
        "19-1-бабы): чатқа «қызметкерді шақырыңыз» деп жазыңыз немесе ресепшенге "
        "хабарласыңыз.\n\n"
        "Толығырақ: Құпиялылық саясаты — [саясат сілтемесі]."
    ),
    "ru": (
        "Я соглашаюсь на обработку моих персональных данных — текстов сообщений, "
        "идентификатора чата, номера комнаты и периода проживания — для "
        "обслуживания моего проживания в отеле.\n\n"
        "Я согласен(на) с тем, что для подготовки ответов тексты сообщений "
        "передаются сервису обработки за пределами Казахстана (Anthropic, США — "
        "страна, не обеспечивающая защиту персональных данных по смыслу ст. 16 "
        "Закона РК «О персональных данных и их защите»).\n\n"
        "Ответы готовит искусственный интеллект. Вы вправе возразить против "
        "автоматизированной обработки и общаться с сотрудником (ст. 19-1 Закона): "
        "напишите в чат «позовите сотрудника» или обратитесь на ресепшен.\n\n"
        "Подробнее: Политика конфиденциальности — [ссылка на политику]."
    ),
    "en": (
        "I consent to the processing of my personal data — message texts, chat "
        "identifier, room number and stay period — for the purpose of serving my "
        "stay at the hotel.\n\n"
        "I agree that, to prepare replies, message texts are transferred to a "
        "processing service outside Kazakhstan (Anthropic, USA — a country that "
        "does not ensure personal data protection within the meaning of Art. 16 of "
        "the Kazakhstan Law “On Personal Data and Its Protection”).\n\n"
        "Replies are prepared by artificial intelligence. You may object to "
        "automated processing and talk to a staff member instead (Art. 19-1 of the "
        "Law): type “call a staff member” in the chat or contact the reception.\n\n"
        "Details: Privacy Policy — [privacy policy URL]."
    ),
}

_BUTTON_LABELS = {"kk": "Келісемін", "ru": "Согласен(на)", "en": "I agree"}


def normalize_language(raw: str | None) -> str | None:
    """Код языка клиента → язык согласия; None — язык неизвестен.

    Клиент присылает BCP-47 (`ru-RU`, `en-GB`) — значим только первый сабтег.
    Незнакомый язык (`de`, `zh`) — это тоже «неизвестно»: показать немцу
    казахский текст хуже, чем показать все три (docs/legal/consent-text.md).
    """
    if not raw:
        return None
    code = raw.strip().lower().split("-")[0]
    return code if code in CONSENT_LANGUAGES else None


def languages_for(language: str | None) -> tuple[str, ...]:
    """Какие версии текста показать: известный язык — одну, иначе все три."""
    return (language,) if language in CONSENT_LANGUAGES else CONSENT_LANGUAGES


def consent_text(language: str | None) -> str:
    """Полный текст согласия для показа гостю (spec 0029 §3).

    Сокращать текст в каналах нельзя (docs/legal/consent-text.md): упоминание
    трансграничной передачи и права по ст. 19-1 — обязательные элементы.
    """
    url = privacy_policy_url()
    parts = [
        _TEXTS[code].replace(_POLICY_PLACEHOLDERS[code], url) for code in languages_for(language)
    ]
    return "\n\n———\n\n".join(parts)


def consent_button_label(language: str | None) -> str:
    """Надпись на кнопке согласия; при неизвестном языке — все три через точку."""
    return " · ".join(_BUTTON_LABELS[code] for code in languages_for(language))


def raw_consent_text(language: str) -> str:
    """Текст согласия БЕЗ подстановки ссылки — опора теста дословности копии."""
    return _TEXTS[language]


def raw_button_label(language: str) -> str:
    """Надпись кнопки одного языка — опора теста дословности копии."""
    return _BUTTON_LABELS[language]


def is_consent_current(version: str | None) -> bool:
    """Согласие действительно? Единственное правило на оба канала (spec 0029 §1).

    Версия отличается от текущей (или согласия нет) — гость проходит гейт
    заново на новом тексте.
    """
    return version == CONSENT_VERSION
