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
# - слово с обычным употреблением — только в связке или с явным щитом от этого
#   употребления: «врач» ловит «есть ли в отеле врач?», «горит» — «не горит
#   свет», «smoke» — «can I smoke here?». Каждый такой щит стоит строкой в
#   `test_ordinary_message_is_not_an_emergency`: без теста он вернётся первой же
#   правкой списка (ревью PR #291);
# - ложное срабатывание дешевле пропуска: гость получает телефон ресепшена,
#   персонал — одно сообщение в чат; пропущенный пожар не стоит ничего
#   сравнимого. Правило действует ТАМ, ГДЕ ФРАЗА ДВУСМЫСЛЕННА; там, где обычное
#   употребление однозначно («не горит», «can I smoke»), оно уступает правилу
#   выше: перехват обрывает ход, и заявки на перегоревшую лампочку не создаётся
#   вовсе.
_PATTERNS: Final[tuple[tuple[EmergencyKind, str, str], ...]] = (
    # --- Пожар, дым, газ -----------------------------------------------------
    (EmergencyKind.FIRE, "ru", r"пожар(?:а|у|ом|е)?\b"),
    # Отрицание — не пожар: «не горит свет/лампочка/телевизор» это самое частое
    # обычное сообщение отеля, а не ЧП (ревью PR #291). «Горит!», «горит
    # проводка», «у меня горит номер» ловятся по-прежнему; «индикатор горит
    # красным» остаётся ложным срабатыванием осознанно — фраза двусмысленна.
    # `\b` перед «не» обязателен: без него щит гасит маркер после хвоста
    # ЛЮБОГО слова на «-не» — «в стене горит проводка», «на кухне горит
    # масло», то есть ровно там, где предложным падежом названо место
    # пожара (ревью PR #291, Н2-1).
    (EmergencyKind.FIRE, "ru", r"(?<!\bне )\bгор(?:ит|им|ю)\b"),
    # Только возвратные формы: «загорелась проводка» — пожар, «я загорел» — нет.
    (EmergencyKind.FIRE, "ru", r"\bзагорел(?:ся|ась|ось|ись)\b"),
    (EmergencyKind.FIRE, "ru", r"\bвозгорани\w*"),
    (EmergencyKind.FIRE, "ru", r"\bдым(?:а|у|ом|е)?\b"),
    (EmergencyKind.FIRE, "ru", r"\bзадымлени\w*"),
    (EmergencyKind.FIRE, "ru", r"\b(?:пахнет газом|запах газа|утечка газа)\b"),
    (EmergencyKind.FIRE, "kk", r"\bөрт\w*"),
    (EmergencyKind.FIRE, "kk", r"\bтүтін\w*"),
    (EmergencyKind.FIRE, "kk", r"\bжанып жатыр\b"),
    (EmergencyKind.FIRE, "kk", r"\bгаз иіс\w*"),
    (EmergencyKind.FIRE, "en", r"\bfire\b"),
    # «Smoke» без спроса разрешения: «can I smoke», «may we smoke», «want to
    # smoke» — вопрос о правилах отеля, а не пожар (ревью PR #291). Во всех этих
    # оборотах перед словом стоит «i »/«we » или «to », поэтому щит короткий.
    # «There is smoke in my room», «smoke everywhere», «I smell smoke» — ловятся.
    (EmergencyKind.FIRE, "en", r"(?<! to )(?<!\bi )(?<!\bwe )\bsmoke\b"),
    (EmergencyKind.FIRE, "en", r"\bburning\b"),
    (EmergencyKind.FIRE, "en", r"\bgas leak\b"),
    # --- Медицина ------------------------------------------------------------
    (EmergencyKind.MEDICAL, "ru", r"\bскор(?:ая|ую)\b"),
    # Врач/доктор — ТОЛЬКО в связке с просьбой: голое слово ловит обычный вопрос
    # «есть ли в отеле врач?», а это не ЧП (проверено на живых фразах 19.08).
    (
        EmergencyKind.MEDICAL,
        "ru",
        r"\b(?:вызов\w+|вызвать|позов\w+|позвать|нужен|нужна|нужно|срочно)\s+"
        r"(?:срочно\s+)?(?:врач|доктор)\w*",
    ),
    (EmergencyKind.MEDICAL, "ru", r"\b(?:врач|доктор)\w*\s+(?:срочно|нужен|нужна)\b"),
    (EmergencyKind.MEDICAL, "ru", r"\bзадыха\w*"),
    (EmergencyKind.MEDICAL, "ru", r"\bне дыш(?:ит|у)\b"),
    (EmergencyKind.MEDICAL, "ru", r"\b(?:без сознания|потерял\w* сознание)\b"),
    (EmergencyKind.MEDICAL, "ru", r"\b(?:сердечный приступ|инфаркт\w*|инсульт\w*)\b"),
    (EmergencyKind.MEDICAL, "ru", r"\bкровотечени\w*"),
    (EmergencyKind.MEDICAL, "ru", r"\bумира(?:ет|ю)\b"),
    # Связка, как у русского «врача» выше: голое слово ловит обычный вопрос
    # «қонақүйде дәрігер бар ма?» («есть ли в отеле врач?») — ревью PR #291.
    (EmergencyKind.MEDICAL, "kk", r"\bдәрігер\w*\s+(?:шақыр\w*|керек|қажет)\b"),
    (EmergencyKind.MEDICAL, "kk", r"\bжедел жәрдем\b"),
    (EmergencyKind.MEDICAL, "kk", r"\bтұншығ\w*"),
    (EmergencyKind.MEDICAL, "kk", r"\bесінен тан\w*"),
    (EmergencyKind.MEDICAL, "kk", r"\bжүрек ұстама\w*"),
    (EmergencyKind.MEDICAL, "en", r"\bambulance\b"),
    # То же и по-английски: «is there a doctor in the hotel?» — не ЧП.
    (
        EmergencyKind.MEDICAL,
        "en",
        r"\b(?:call|need|needs|send|get|want)\s+(?:a\s+|an\s+|the\s+)?doctor\b"
        r"|\bdoctor\s+(?:urgently|now|asap)\b",
    ),
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
    # Прямая просьба вызвать полицию или охрану: гость уже сам назвал, что нужно
    # (находка основателя 19.08 на сценарии «пьяные на этаже»).
    (EmergencyKind.SECURITY, "ru", r"\b(?:полици\w+|охран(?:у|а|ы|ник\w*))\b"),
    (EmergencyKind.SECURITY, "kk", r"\b(?:полици\w+|күзет\w*)\b"),
    (EmergencyKind.SECURITY, "en", r"\b(?:police|security guard)\b"),
    (EmergencyKind.SECURITY, "kk", r"\bшабуыл\w*"),
    (EmergencyKind.SECURITY, "kk", r"\bтона(?:п|ды|у)\w*"),
    # Только глагольные формы («қорқытып жатыр», «қорқытты»): голый «Қорқыт» —
    # имя собственное, аэропорт Кызылорды «Қорқыт Ата» (ревью PR #291).
    (EmergencyKind.SECURITY, "kk", r"\bқорқыт(?:ып|ты|ады|ам|са|у|уда)\w*"),
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

# Второй абзац: только у ЧП-перехвата. Смысл — «не сиди и не жди ответа бота»,
# а не номер: гость находится В ОТЕЛЕ, у него телефон в номере, сотрудник на
# этаже и минута до стойки. Номеров экстренных служб здесь нет намеренно
# (решение основателя 19.08): внешние службы вызывает ресепшен, а не гость, —
# для половины случаев (шумные соседи, кража) совет «звоните 112» прямо неверен.
_ACT_NOW: Final[dict[str, str]] = {
    "ru": (
        "Не ждите ответа в чате: позвоните на ресепшен с телефона в номере "
        "или скажите любому сотруднику рядом."
    ),
    "kk": (
        "Чаттағы жауапты күтпеңіз: бөлмедегі телефоннан ресепшнге қоңырау шалыңыз "
        "немесе жаныңыздағы кез келген қызметкерге айтыңыз."
    ),
    "en": (
        "Don't wait for a reply here: call reception from your room phone "
        "or tell any staff member nearby."
    ),
}
_RECEPTION_LABEL: Final[dict[str, str]] = {
    "ru": "Ресепшен",
    "kk": "Ресепшн",
    "en": "Reception",
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
    """Текст ЧП-перехвата (spec 0034 §3).

    `reception_phone` — `TenantConfig.reception_phone`; не задан — строки с
    номером нет вовсе («📞 Ресепшен: —» это не контакт), но сам текст остаётся
    действенным: инструкция второго абзаца от настроек не зависит.
    """
    code = _language_or_fallback(language)
    lines = [_URGENT_ACCEPTED[code], "", _ACT_NOW[code]]
    if reception_phone:
        lines.append(f"📞 {_RECEPTION_LABEL[code]}: {reception_phone}")
    return "\n".join(lines)


def _language_or_fallback(language: str | None) -> str:
    """Код языка, для которого у нас есть текст; иначе русский (см. шапку)."""
    return language if language in _URGENT_ACCEPTED else FALLBACK_LANGUAGE
