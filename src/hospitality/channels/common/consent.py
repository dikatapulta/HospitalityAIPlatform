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
CONSENT_VERSION = "2026-07-29-v2"

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
        "Сізге қонақүйдің ЖИ-консьержі жауап береді; кез келген сәтте қызметкерді "
        "шақыра аласыз. Жалғастыра отырып, сіз дербес деректеріңіздің Құпиялылық "
        "саясатына сәйкес өңделуіне келісесіз: [саясат сілтемесі]"
    ),
    "ru": (
        "Вам отвечает ИИ-консьерж отеля; в любой момент можно позвать сотрудника. "
        "Продолжая, вы соглашаетесь с обработкой персональных данных согласно "
        "Политике конфиденциальности: [ссылка на политику]"
    ),
    "en": (
        "You are chatting with the hotel’s AI concierge; you can ask for a staff "
        "member at any time. By continuing, you agree to the processing of personal "
        "data under the Privacy Policy: [privacy policy URL]"
    ),
}

_BUTTON_LABELS = {"kk": "Жалғастыру", "ru": "Продолжить", "en": "Continue"}


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
    """Текст экрана согласия для показа гостю (spec 0029 §3, изменение v2).

    С v2 (решение основателя 29.07) экран компактный: одна строка + ссылка на
    политику. Обязательные элементы (трансграничная передача — ст. 16, право на
    сотрудника — ст. 19-1) раскрыты в политике по ссылке; достаточность такой
    формы — на проверке юриста (FOUNDER_REVIEW_QUEUE). Каналы показывают текст
    целиком, не сокращая (docs/legal/consent-text.md).
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
