"""Инструмент «Предложить идею»: черновик карточки идеи (spec 0071 §3 п. 2, §6).

Обёртка по канону `create_service_request` над `mentor.save_idea_draft`: модель
предлагает пересказ идеи и пометку, а черновик пишет `ai/mentor/ideas.py`. Класс
`auto` (P-9): черновик ничего не отправляет — карточку отправляет кнопкой сам
автор, поправив пересказ. Вызов не принудительный: текстом модель задаёт
уточняющий вопрос; принудительный — только на пределе сообщений (§3 п. 5).

Пометка и её пояснение — только основателю: автору их не показывает ни ход, ни
кабинет (§6). Кривые аргументы — `None`: хода с черновиком не будет.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools.base import ConfirmationClass
from hospitality.modules.mentor.api import IdeaMark

NAME: Final = "propose_idea_card"
CONFIRMATION_CLASS: Final = ConfirmationClass.AUTO
CREATES_REQUEST: Final = False

# Пересказ — 2–6 предложений; предел — с запасом, длиннее — не карточка, а стенограмма.
MAX_SUMMARY_CHARS: Final = 3000
MAX_MARK_NOTE_CHARS: Final = 600

_DESCRIPTION: Final = (
    "Propose the idea card the employee will review, edit and send to the developer. "
    "Call it when you understand the idea; the new card replaces the previous one."
)


class IdeaCardProposal(BaseModel):
    """Аргументы вызова после проверки: пустой пересказ — не карточка."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    mark: IdeaMark
    mark_note: str | None = Field(default=None, max_length=MAX_MARK_NOTE_CHARS)

    @field_validator("summary", "mark_note", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value


def build_spec() -> ToolSpec:
    return ToolSpec(
        name=NAME,
        description=_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "maxLength": MAX_SUMMARY_CHARS,
                    "description": (
                        "The idea in the employee's language, 2-6 sentences, no names of "
                        "guests or colleagues, no promises."
                    ),
                },
                "mark": {
                    "type": "string",
                    "enum": [mark.value for mark in IdeaMark],
                    "description": "For the developer only: the employee never sees it.",
                },
                "mark_note": {
                    "type": "string",
                    "maxLength": MAX_MARK_NOTE_CHARS,
                    "description": (
                        "For the developer only, always in Russian: where it already is, "
                        "how it differs, or which limit it hits."
                    ),
                },
            },
            "required": ["summary", "mark", "mark_note"],
        },
    )


def parse(arguments: Mapping[str, Any]) -> IdeaCardProposal | None:
    """Аргументы модели → предложение; невалидные — `None`."""
    try:
        return IdeaCardProposal.model_validate(dict(arguments))
    except ValidationError:
        return None
