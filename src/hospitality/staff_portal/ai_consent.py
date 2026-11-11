"""Отметка согласия сотрудника на ИИ-раздел в кабинете (spec 0082 §2.3, issue #985).

Раздел знает своего получателя (`SECTION_RECIPIENTS` — пункты 2 и 3 бланка согласия), а
проверка одна — `require` в `require_section` раздела: через неё идут его страницы и
JSON-действия, а меню и главная спрашивают `has`. Отметку ставит менеджер в «Сотрудниках»
(`team.py`, `team_api.py`); суперадмину она не нужна (`StaffContext.has_ai_consent`).
Тексты — spec 0082 §3, запись founder-review PR #1003.
"""

from __future__ import annotations

import enum
from typing import Final

from hospitality.platform.config import TenantConfig
from hospitality.platform.models import AiRecipient
from hospitality.platform.staff_auth import ERR_AUTH_FORBIDDEN, StaffContext
from hospitality.shared.errors import AppError
from hospitality.shared.humanize import plural
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


class AiSection(enum.StrEnum):
    """ИИ-раздел кабинета, который отдаёт тексты сотрудника получателю в США."""

    MENTOR = "mentor"
    IDEAS = "ideas"
    # Кнопка «Оформить» в служебной записке (spec 0077 §7, PR 6).
    MEMO_FORMAT = "memo_format"


SECTION_RECIPIENTS: Final[dict[AiSection, AiRecipient]] = {
    AiSection.MENTOR: AiRecipient.OPENAI,
    AiSection.IDEAS: AiRecipient.ANTHROPIC,
    AiSection.MEMO_FORMAT: AiRecipient.ANTHROPIC,
}

# Тексты (spec 0082 §3; показаны основателю 11.11 файлом диспетчеру).
SECTION_TITLES: Final[dict[AiSection, str]] = {
    AiSection.MENTOR: "«Наставник»",
    AiSection.IDEAS: "«Предложить идею»",
    AiSection.MEMO_FORMAT: "«Оформить»",
}
RECIPIENT_NAMES: Final[dict[AiRecipient, str]] = {
    AiRecipient.OPENAI: "OpenAI (США)",
    AiRecipient.ANTHROPIC: "Anthropic (США)",
}
# Галочки строки «Сотрудников» — по пункту бланка.
RECIPIENT_CHIPS: Final[dict[AiRecipient, str]] = {
    AiRecipient.OPENAI: "Наставник — OpenAI",
    AiRecipient.ANTHROPIC: "Идеи и «Оформить» — Anthropic",
}
CLOSED_TITLE: Final = "Раздел закрыт"
CLOSED_TEXT: Final = (
    "{section} открывается после вашего письменного согласия на передачу данных в {recipient}. "
    "Подписали — менеджер отметит это в «Сотрудниках»."
)
TEAM_LEGEND: Final = "Согласие на ИИ подписано:"
TEAM_HINT: Final = (
    "Согласие на ИИ: отметьте, когда у вас на руках согласие сотрудника, подписанное по пункту "
    "2 или 3. Без отметки раздел ему не виден. Сотрудник отозвал согласие — снимите отметку и "
    "напишите на privacy@necturn.com: удалим его данные в разделе."
)
TEAM_SAVED: Final = "Сохранено"
TEAM_FAILED: Final = "Не получилось — обновите страницу и попробуйте ещё раз."
RESULTS_NOTE: Final = (
    "Без отметки согласия на «Наставника» — {count} {people}: экзамен им не виден, в итогах их нет."
)


class AiConsentMissingError(AppError):
    """Отметки нет: JSON-действие отвечает 403 ERR-AUTH-003, страница — «Раздел закрыт»."""

    def __init__(self, section: AiSection) -> None:
        super().__init__(
            code=ERR_AUTH_FORBIDDEN,
            message="No consent mark for the recipient of this AI section",
            status_code=403,
        )
        self.section = section


def has(staff: StaffContext, section: AiSection) -> bool:
    """Отметка получателя раздела есть (или это суперадмин) — для меню и кнопок."""
    return staff.has_ai_consent(SECTION_RECIPIENTS[section])


def require(staff: StaffContext, section: AiSection) -> None:
    """Проверка раздела до любого вызова модели; отметки нет — `AiConsentMissingError`."""
    if has(staff, section):
        return
    logger.warning(
        "staff.ai_consent_missing",
        user_id=str(staff.user_id),
        section=section.value,
        recipient=SECTION_RECIPIENTS[section].value,
    )
    raise AiConsentMissingError(section)


def closed_context(section: AiSection) -> dict[str, str]:
    """Контекст страницы «Раздел закрыт» (`ai_consent_closed.html`)."""
    return {
        "closed_title": CLOSED_TITLE,
        "closed_text": CLOSED_TEXT.format(
            section=SECTION_TITLES[section],
            recipient=RECIPIENT_NAMES[SECTION_RECIPIENTS[section]],
        ),
    }


def team_recipients(config: TenantConfig | None) -> list[AiRecipient]:
    """Получатели, чьи разделы включены у отеля, — их галочки в «Сотрудниках» (§2.2).

    Флаги — те же, что `section_on` разделов (`mentor`, `ideas`, `memos`): модули
    разделов импортируют `team`, обратный импорт замкнул бы цикл."""
    if config is None:
        return []
    shown = []
    if config.mentor_enabled:
        shown.append(AiRecipient.OPENAI)
    if config.ideas_enabled or config.memos_enabled:
        shown.append(AiRecipient.ANTHROPIC)
    return shown


def results_note(count: int) -> str | None:
    """Строка над «Итогами экзаменов»: сколько действующих сотрудников без отметки."""
    if not count:
        return None
    people = plural(count, "сотрудник", "сотрудника", "сотрудников")
    return RESULTS_NOTE.format(count=count, people=people)
