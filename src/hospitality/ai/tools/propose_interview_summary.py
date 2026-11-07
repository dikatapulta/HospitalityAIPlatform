"""Инструмент интервью служб: итог разговора по темам (spec 0071 §4).

Тот же канон, что у `propose_idea_card`: итог — черновик, отправляет его
кнопкой сам сотрудник, поправив текст; класс `auto` (P-9). Пометки у итога
интервью нет (§4). Кривые аргументы — `None`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hospitality.ai.gateway.api import ToolSpec
from hospitality.ai.tools.base import ConfirmationClass

NAME: Final = "propose_interview_summary"
CONFIRMATION_CLASS: Final = ConfirmationClass.AUTO
CREATES_REQUEST: Final = False

# Итог по шести темам — до ~1500 токенов ответа (§4); предел знаков — с запасом.
MAX_SUMMARY_CHARS: Final = 8000

_DESCRIPTION: Final = (
    "Finish the interview: propose the summary by topic that the employee will review, "
    "edit and send. The new summary replaces the previous one."
)


class InterviewSummaryProposal(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)

    @field_validator("summary", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


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
                        "In the employee's language, by topic: a short heading per topic and "
                        "2-5 points with their examples and numbers. No names, no proposals."
                    ),
                },
            },
            "required": ["summary"],
        },
    )


def parse(arguments: Mapping[str, Any]) -> InterviewSummaryProposal | None:
    try:
        return InterviewSummaryProposal.model_validate(dict(arguments))
    except ValidationError:
        return None
