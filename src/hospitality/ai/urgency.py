"""Срочность в гостевом диалоге: ЧП-перехват без LLM и тексты (spec 0034, issue #208).

Лист-модуль без ввода-вывода: чистые функции над строкой. LLM здесь не
вызывается и вызываться не может — в этом весь смысл перехвата: путь «гость
написал о пожаре → персонал узнал» не должен зависеть ни от доступности
провайдера, ни от дневного бюджета тенанта, ни от того, что модель сегодня
решит про это сообщение. Как `escalation.py`, модуль числится в контракте 4
import-linter обязанностью анатомии `ai/`.

Две половины задачи #208 живут здесь вместе, потому что делят один
утверждённый абзац текста (§3 спеки): первый абзац получает и гость, чьё
сообщение перехвачено как ЧП, и гость, чья срочная заявка создана без гейта
подтверждения (ADR-018). Разводить их по двум модулям значило бы завести две
копии формулировки, которые однажды разойдутся.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Final

# Языки статических текстов гостя — те же три, что у остальных констант канала
# (решение основателя 06.08, аудит §12 п. 6). Язык, которого здесь нет,
# деградирует на русский: платформа исходно русскоязычная.
FALLBACK_LANGUAGE: Final = "ru"

# Единый номер экстренных служб Казахстана. Платформа работает в РК (ADR-006);
# отель за её пределами — повод для поля конфигурации, а не для второго текста.
EMERGENCY_NUMBER: Final = "112"


class EmergencyKind(enum.StrEnum):
    """Вид ЧП — различается только для логов и метрик (spec 0034 §1).

    Гость и персонал видят одно: «это срочно, персоналу передано». Вид нужен,
    чтобы по логам было видно, на чём перехват срабатывает и не начал ли он
    ловить лишнее.
    """

    FIRE = "fire"
    MEDICAL = "medical"
    SECURITY = "security"


@dataclass(frozen=True)
class EmergencyMatch:
    """Сработавший маркер ЧП.

    `language` — язык САМОГО МАРКЕРА, а не гостя: языка гостя без LLM система
    не знает, но знает, на каком языке написано слово, которое совпало
    (spec 0034 §2). На нём и отвечаем.
    """

    kind: EmergencyKind
    language: str
    marker: str


# Маркеры ЧП: (вид, язык, регулярка). Порядок значим — возвращается ПЕРВОЕ
# совпадение. Правила составления списка (spec 0034 §1):
#
# - слово целиком: «пожар» не ловит «пожарный выход» (там \b не сработает —
#   после «пожар» идёт буква), «emergency» отсечён от «emergency exit» явным
#   отрицательным просмотром вперёд;
# - никаких общих слов помощи («помогите», «көмек», «help»): в чате отеля это
#   чаще всего просьба разобраться с телевизором;
# - ложное срабатывание дешевле пропуска: гость получает телефоны, персонал —
#   одно сообщение в чат; пропущенный пожар не стоит ничего сравнимого.
_PATTERNS: Final[tuple[tuple[EmergencyKind, str, str], ...]] = (
    # --- Пожар, дым, газ -----------------------------------------------------
    (EmergencyKind.FIRE, "ru", r"пожар(?:а|у|ом|е)?\b"),
    (EmergencyKind.FIRE, "ru", r"\bгор(?:ит|им|ю)\b"),
    (EmergencyKind.FIRE, "ru", r"\bзагорел\w*"),
    (EmergencyKind.FIRE, "ru", r"\bвозгорани\w*"),
    (EmergencyKind.FIRE, "ru", r"\bдым(?:а|у|ом|е)?\b"),
    (EmergencyKind.FIRE, "ru", r"\bзадымлени\w*"),
    (EmergencyKind.FIRE, "ru", r"\b(?:пахнет газом|запах газа|утечка газа)\b"),
    (EmergencyKind.FIRE, "kk", r"\bөрт\w*"),
    (EmergencyKind.FIRE, "kk", r"\bтүтін\w*"),
    (EmergencyKind.FIRE, "kk", r"\bжанып жатыр\b"),
    (EmergencyKind.FIRE, "kk", r"\bгаз иіс\w*"),
    (EmergencyKind.FIRE, "en", r"\bfire\b"),
    (EmergencyKind.FIRE, "en", r"\bsmoke\b"),
    (EmergencyKind.FIRE, "en", r"\bburning\b"),
    (EmergencyKind.FIRE, "en", r"\bgas leak\b"),
    # --- Медицина ------------------------------------------------------------
    (EmergencyKind.MEDICAL, "ru", r"\bскор(?:ая|ую)\b"),
    (EmergencyKind.MEDICAL, "ru", r"\bврач(?:а|у|ом|е)?\b"),
    (EmergencyKind.MEDICAL, "ru", r"\bдоктор(?:а|у|ом|е)?\b"),
    (EmergencyKind.MEDICAL, "ru", r"\bзадыха\w*"),
    (EmergencyKind.MEDICAL, "ru", r"\bне дыш(?:ит|у)\b"),
    (EmergencyKind.MEDICAL, "ru", r"\b(?:без сознания|потерял\w* сознание)\b"),
    (EmergencyKind.MEDICAL, "ru", r"\b(?:сердечный приступ|инфаркт\w*|инсульт\w*)\b"),
    (EmergencyKind.MEDICAL, "ru", r"\bкровотечени\w*"),
    (EmergencyKind.MEDICAL, "ru", r"\bумира(?:ет|ю)\b"),
    (EmergencyKind.MEDICAL, "kk", r"\bдәрігер\w*"),
    (EmergencyKind.MEDICAL, "kk", r"\bжедел жәрдем\b"),
    (EmergencyKind.MEDICAL, "kk", r"\bтұншығ\w*"),
    (EmergencyKind.MEDICAL, "kk", r"\bесінен тан\w*"),
    (EmergencyKind.MEDICAL, "kk", r"\bжүрек ұстама\w*"),
    (EmergencyKind.MEDICAL, "en", r"\bambulance\b"),
    (EmergencyKind.MEDICAL, "en", r"\bdoctor\b"),
    (EmergencyKind.MEDICAL, "en", r"\bheart attack\b"),
    (EmergencyKind.MEDICAL, "en", r"\b(?:can'?t|cannot) breathe\b"),
    (EmergencyKind.MEDICAL, "en", r"\bunconscious\b"),
    (EmergencyKind.MEDICAL, "en", r"\bchoking\b"),
    (EmergencyKind.MEDICAL, "en", r"\bbleeding\b"),
    (EmergencyKind.MEDICAL, "en", r"\bemergency\b(?!\s+exit)"),
    # --- Безопасность --------------------------------------------------------
    (EmergencyKind.SECURITY, "ru", r"\bнапал\w*"),
    (EmergencyKind.SECURITY, "ru", r"\bнападени\w*"),
    (EmergencyKind.SECURITY, "ru", r"\b(?:ограбил\w*|грабят)\b"),
    (EmergencyKind.SECURITY, "ru", r"\bугрожа\w*"),
    (EmergencyKind.SECURITY, "ru", r"\b(?:драка|дерутся|избили|бьют)\b"),
    (EmergencyKind.SECURITY, "ru", r"\bспасите\b"),
    (EmergencyKind.SECURITY, "kk", r"\bшабуыл\w*"),
    (EmergencyKind.SECURITY, "kk", r"\bтона(?:п|ды|у)\w*"),
    (EmergencyKind.SECURITY, "kk", r"\bқорқыт\w*"),
    (EmergencyKind.SECURITY, "kk", r"\bтөбелес\w*"),
    (EmergencyKind.SECURITY, "en", r"\battack(?:ed|ing)?\b"),
    (EmergencyKind.SECURITY, "en", r"\brobb(?:ed|ery|ing)\b"),
    (EmergencyKind.SECURITY, "en", r"\bthreaten\w*"),
    (EmergencyKind.SECURITY, "en", r"\bbreak[- ]?in\b"),
)

# Компилируется один раз на процесс: перехват стоит на КАЖДОМ сообщении гостя.
_COMPILED: Final[tuple[tuple[EmergencyKind, str, re.Pattern[str]], ...]] = tuple(
    (kind, language, re.compile(pattern, re.IGNORECASE)) for kind, language, pattern in _PATTERNS
)

# Первый абзац утверждённого текста (аудит §12 п. 5). Общий у двух путей: его же
# получает гость, чья срочная заявка создана без гейта подтверждения (ADR-018).
_URGENT_ACCEPTED: Final[dict[str, str]] = {
    "ru": "Понял, это срочно. Уже передаю персоналу отеля.",
    "kk": "Түсіндім, бұл шұғыл. Қазір қонақүй қызметкерлеріне хабарлап жатырмын.",
    "en": "Understood — this is urgent. I'm passing it to the hotel staff right now.",
}

# Второй абзац: только у ЧП-перехвата. Заявке о течи телефон службы спасения
# ни к чему, а гостю в дыму чат отеля — не тот канал, и он обязан это услышать.
_LIFE_THREAT_INTRO: Final[dict[str, str]] = {
    "ru": "Если есть угроза жизни или здоровью — не ждите ответа в чате:",
    "kk": "Егер өмірге немесе денсаулыққа қауіп төнсе, чаттағы жауапты күтпеңіз:",
    "en": "If anyone's life or health is at risk, don't wait for a reply here:",
}
_RECEPTION_LABEL: Final[dict[str, str]] = {
    "ru": "Ресепшен",
    "kk": "Ресепшн",
    "en": "Reception",
}
_EMERGENCY_SERVICE_LABEL: Final[dict[str, str]] = {
    "ru": "служба спасения",
    "kk": "құтқару қызметі",
    "en": "emergency services",
}


def detect_emergency(text: str) -> EmergencyMatch | None:
    """Первый сработавший маркер ЧП в тексте гостя; None — обычное сообщение.

    Чистая функция: ни ввода-вывода, ни LLM, ни состояния. Зовётся на каждом
    сообщении гостя ДО всего остального (`channels/common/guest_turn.py`).
    """
    for kind, language, pattern in _COMPILED:
        found = pattern.search(text)
        if found is not None:
            return EmergencyMatch(kind=kind, language=language, marker=found.group(0))
    return None


def urgent_accepted_reply(language: str | None) -> str:
    """«Понял, это срочно. Уже передаю персоналу отеля.» на языке гостя.

    Реплика гостю, чья СРОЧНАЯ заявка создана без гейта подтверждения
    (ADR-018): текста модели на таком ходу показывать нельзя — промпт требует
    от неё вопроса-подтверждения, а вопрос о уже созданной заявке был бы ложью.
    """
    return _URGENT_ACCEPTED[_language_or_fallback(language)]


def emergency_reply(language: str | None, reception_phone: str | None) -> str:
    """Утверждённый текст ЧП-перехвата (spec 0034 §3).

    `reception_phone` — `TenantConfig.reception_phone`. Не задан — строки
    ресепшена нет вовсе: «📞 Ресепшен: —» это не контакт, а издевательство;
    строка службы спасения остаётся в любом случае.
    """
    code = _language_or_fallback(language)
    lines = [_URGENT_ACCEPTED[code], "", _LIFE_THREAT_INTRO[code]]
    if reception_phone:
        lines.append(f"📞 {_RECEPTION_LABEL[code]}: {reception_phone}")
    lines.append(f"📞 {EMERGENCY_NUMBER} — {_EMERGENCY_SERVICE_LABEL[code]}")
    return "\n".join(lines)


def _language_or_fallback(language: str | None) -> str:
    """Код языка, для которого у нас есть текст; иначе русский (см. шапку)."""
    return language if language in _URGENT_ACCEPTED else FALLBACK_LANGUAGE
