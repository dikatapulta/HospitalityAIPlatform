"""«Тренировка»: вопрос по статье кодекса и проверка ответа (spec 0048 §5.5).

«Новый вопрос» выбирает статью (глава, тема свободным текстом или любая) и просит
модель задать по ней один практический вопрос. «Проверить» сравнивает ответ
сотрудника со статьёй; первая строка проверки «Оценка: верно / частично верно /
неверно» даёт строку статистики. Оценки нет — она показана, но в статистику не идёт.
Записи — только после конца потока: оборванная проверка оставляет вопрос открытым.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Final

from pydantic import BaseModel, Field

from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.common import (
    MentorDelta,
    MentorDone,
    MentorEvent,
    MentorProviders,
    article_block,
    embed_query,
    ensure_article_embeddings,
    require_codex,
    require_text,
    source_of,
    stream_reply,
)
from hospitality.ai.prompts import load_prompt
from hospitality.modules.mentor import api as mentor
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

QUESTION_PROMPT: Final = "mentor_quiz_question_v1"
CHECK_PROMPT: Final = "mentor_quiz_check_v1"
QUESTION_MAX_TOKENS: Final = 300
CHECK_MAX_TOKENS: Final = 500

# Та же строка, что у цветной метки в чате «Наставника» (`quiz_stats.py`): считаем
# то, что увидел сотрудник. Звёздочки — модель иногда выделяет оценку жирным.
_GRADE = re.compile(r"^\s*\**Оценка:?\**\s*\**(частично верно|неверно|верно)", re.IGNORECASE)
_GRADES: Final = {
    "верно": mentor.QuizGrade.CORRECT,
    "частично верно": mentor.QuizGrade.PARTIAL,
    "неверно": mentor.QuizGrade.WRONG,
}


class QuizTopic(BaseModel):
    """Тема «Нового вопроса»: глава (в том числе глава должности), свободный текст или ничего."""

    chapter_number: int | None = Field(default=None, ge=1)
    text: str | None = Field(default=None, max_length=200)


def parse_grade(verdict: str) -> mentor.QuizGrade | None:
    """«Оценка: частично верно…» → `PARTIAL`; оценки в начале текста нет — `None`."""
    match = _GRADE.match(verdict)
    return _GRADES[match.group(1).lower()] if match else None


async def new_quiz_question(
    user_id: uuid.UUID, topic: QuizTopic, *, providers: MentorProviders | None = None
) -> AsyncGenerator[MentorEvent, None]:
    """Вопрос потоком; под вопросом источника нет — он выдал бы ответ."""
    providers = providers or MentorProviders()
    await require_codex()
    conversation = await mentor.get_conversation(user_id, mentor.ConversationMode.QUIZ)
    recent = conversation.quiz_recent_articles if conversation else []
    near: list[float] | None = None
    if topic.text and topic.text.strip():
        await ensure_article_embeddings(providers)
        near = await embed_query(topic.text.strip(), providers)
    article = await mentor.pick_quiz_article(
        chapter_number=topic.chapter_number, near=near, exclude=recent
    )
    if article is None:  # в выбранной главе статей нет (кодекс перезагружен) — любая
        article = await mentor.pick_quiz_article(chapter_number=None, near=near, exclude=recent)
    if article is None:  # pragma: no cover — у загруженного кодекса статьи есть всегда
        raise AppError(
            code=mentor.ERR_MENTOR_CODEX_MISSING, message="Codex has no articles", status_code=409
        )
    request = gateway.LlmRequest(
        system=load_prompt(QUESTION_PROMPT).strip(),
        messages=[gateway.LlmMessage(role="user", content=article_block(article))],
        max_tokens=QUESTION_MAX_TOKENS,
        purpose="mentor",
    )
    response: gateway.LlmResponse | None = None
    async with aclosing(stream_reply(request, providers)) as replies:
        async for item in replies:
            if isinstance(item, MentorDelta):
                yield item
            else:
                response = item
    question = require_text(response, flow="quiz_question")
    await mentor.start_quiz_question(
        user_id,
        article_number=article.number,
        question=mentor.NewMentorMessage(
            role=mentor.MessageRole.ASSISTANT, text=question, created_at=utc_now()
        ),
    )
    logger.info("mentor_quiz_question_asked", article_number=article.number)
    yield MentorDone(answer=question, sources=[])


async def check_quiz_answer(
    user_id: uuid.UUID, answer: str, *, providers: MentorProviders | None = None
) -> AsyncGenerator[MentorEvent, None]:
    """Проверка ответа потоком; под проверкой — статья, по которой был вопрос.

    Нет открытого вопроса или его статьи больше нет в кодексе — ERR-MENTOR-003.
    """
    providers = providers or MentorProviders()
    text = answer.strip()
    if not text:
        raise ValueError("check_quiz_answer: пустой ответ")
    answered_at = utc_now()
    article_number, question = await mentor.active_quiz_question(user_id)
    article = (await mentor.get_articles([article_number])).get(article_number)
    if article is None:
        raise AppError(
            code=mentor.ERR_MENTOR_NO_QUIZ_QUESTION,
            message="Training question refers to an article that is no longer in the codex",
            status_code=409,
        )
    payload = {"статья": article_block(article), "вопрос": question, "ответ_сотрудника": text}
    request = gateway.LlmRequest(
        system=load_prompt(CHECK_PROMPT).strip(),
        messages=[gateway.LlmMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
        max_tokens=CHECK_MAX_TOKENS,
        purpose="mentor",
    )
    response: gateway.LlmResponse | None = None
    async with aclosing(stream_reply(request, providers)) as replies:
        async for item in replies:
            if isinstance(item, MentorDelta):
                yield item
            else:
                response = item
    verdict = require_text(response, flow="quiz_check")
    grade = parse_grade(verdict)
    if grade is None:
        logger.warning("mentor_quiz_grade_missing", article_number=article.number)
    await mentor.finish_quiz_answer(
        user_id,
        article=article,
        answer=mentor.NewMentorMessage(
            role=mentor.MessageRole.USER, text=text, created_at=answered_at
        ),
        verdict=mentor.NewMentorMessage(
            role=mentor.MessageRole.ASSISTANT,
            text=verdict,
            cited_articles=[article.number],
            created_at=utc_now(),
        ),
        grade=grade,
    )
    yield MentorDone(answer=verdict, sources=[source_of(article)])
