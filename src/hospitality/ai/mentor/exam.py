"""Экзамен «Наставника» — LLM-часть (spec 0048 §12).

Вопрос и проверка — тем же ходом, что «Тренировка» (`quiz.question_request`,
`quiz.check_request`, `quiz.parse_grade`); попытка, счёт и итог — модуль `mentor`.
Вопрос — статья темы попытки (глава или весь кодекс), которую в этой попытке ещё
не спрашивали (`pick_exam_article`); спрошены все — повтор. Записи — только после
конца потока, как в «Тренировке»: оборванный вопрос не открывается, оборванная
проверка оставляет вопрос открытым. Последнее событие — `done` с ходом экзамена `exam`.

Ответ приходит с попыткой и номером хода, которые видел сотрудник: вопрос сменился
(вторая вкладка, новая попытка) — ERR-MENTOR-004, и ответ не проверяется вовсе.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import aclosing

from pydantic import BaseModel

from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.common import (
    MentorDelta,
    MentorDone,
    MentorEvent,
    MentorProviders,
    require_text,
    source_of,
    stream_reply,
)
from hospitality.ai.mentor.quiz import check_request, parse_grade, question_request
from hospitality.modules.mentor import api as mentor
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


class ExamProgress(BaseModel):
    """Ход попытки для страницы: с `attempt_id` и `turn` сотрудник присылает ответ."""

    attempt_id: uuid.UUID
    turn: int
    answered: int
    total: int
    counted: bool
    finished: bool
    percent: int | None
    passed: bool | None
    correct: int
    partial: int
    wrong: int
    pass_percent: int

    @classmethod
    def of(cls, attempt: mentor.ExamAttemptRead, *, counted: bool = True) -> ExamProgress:
        """`counted=False` — ответ без разобранной оценки: не засчитан, будет другой вопрос."""
        return cls(
            attempt_id=attempt.id,
            turn=attempt.turn,
            answered=attempt.answered,
            total=attempt.question_count,
            counted=counted,
            finished=attempt.finished,
            percent=attempt.percent,
            passed=attempt.passed,
            correct=attempt.correct_count,
            partial=attempt.partial_count,
            wrong=attempt.wrong_count,
            pass_percent=attempt.pass_percent,
        )


class ExamDone(MentorDone):
    """`done` экзамена: к вопросу или проверке — ход попытки."""

    exam: ExamProgress


async def next_exam_question(
    user_id: uuid.UUID, attempt_id: uuid.UUID, *, providers: MentorProviders | None = None
) -> AsyncGenerator[MentorEvent, None]:
    """Следующий вопрос потоком; вопрос уже открыт или попытка сменилась — ERR-MENTOR-004."""
    providers = providers or MentorProviders()
    attempt = await _open_attempt(user_id, attempt_id)
    if attempt.current_question is not None:
        raise _no_exam_question()
    exclude = attempt.asked_articles
    article = await mentor.pick_exam_article(chapter_number=attempt.chapter_number, exclude=exclude)
    if article is None:  # главы нет в перезагруженном кодексе — любая статья
        article = await mentor.pick_exam_article(chapter_number=None, exclude=exclude)
    if article is None:  # pragma: no cover — попытка начинается только при загруженном кодексе
        raise AppError(
            code=mentor.ERR_MENTOR_CODEX_MISSING, message="Codex has no articles", status_code=409
        )
    response: gateway.LlmResponse | None = None
    async with aclosing(stream_reply(question_request(article), providers)) as replies:
        async for item in replies:
            if isinstance(item, MentorDelta):
                yield item
            else:
                response = item
    question = require_text(response, flow="exam_question")
    attempt = await mentor.set_exam_question(
        user_id, attempt.id, turn=attempt.turn, article_number=article.number, question=question
    )
    logger.info("mentor_exam_question_asked", turn=attempt.turn, article_number=article.number)
    yield ExamDone(answer=question, sources=[], exam=ExamProgress.of(attempt))


async def check_exam_answer(
    user_id: uuid.UUID,
    answer: str,
    *,
    attempt_id: uuid.UUID,
    turn: int,
    providers: MentorProviders | None = None,
) -> AsyncGenerator[MentorEvent, None]:
    """Проверка ответа потоком; под проверкой — статья вопроса, в `done` — счёт или итог.

    Статьи вопроса нет в перезагруженном кодексе — вопрос снимается без счёта, а
    сотрудник получает ERR-MENTOR-004: после обновления страницы — следующий вопрос.
    """
    providers = providers or MentorProviders()
    text = answer.strip()
    if not text:
        raise ValueError("check_exam_answer: пустой ответ")
    attempt = await _open_attempt(user_id, attempt_id)
    number = attempt.current_article_number
    if attempt.turn != turn or number is None or attempt.current_question is None:
        raise _no_exam_question()
    article = (await mentor.get_articles([number])).get(number)
    if article is None:
        await mentor.drop_exam_question(user_id, attempt.id, turn=turn)
        raise _no_exam_question()
    response: gateway.LlmResponse | None = None
    request = check_request(article, question=attempt.current_question, answer=text)
    async with aclosing(stream_reply(request, providers)) as replies:
        async for item in replies:
            if isinstance(item, MentorDelta):
                yield item
            else:
                response = item
    verdict = require_text(response, flow="exam_check")
    grade = parse_grade(verdict)
    if grade is None:
        logger.warning("mentor_exam_grade_missing", article_number=article.number)
    attempt = await mentor.record_exam_answer(
        user_id, attempt.id, turn=turn, article_number=article.number, grade=grade
    )
    yield ExamDone(
        answer=verdict,
        sources=[source_of(article)],
        exam=ExamProgress.of(attempt, counted=grade is not None),
    )


async def _open_attempt(user_id: uuid.UUID, attempt_id: uuid.UUID) -> mentor.ExamAttemptRead:
    """Идущая попытка сотрудника, если это та, что он видел; иначе ERR-MENTOR-004."""
    attempt = await mentor.current_exam(user_id)
    if attempt is None or attempt.id != attempt_id:
        raise _no_exam_question()
    return attempt


def _no_exam_question() -> AppError:
    return AppError(
        code=mentor.ERR_MENTOR_NO_EXAM_QUESTION,
        message="Exam question is no longer open: the attempt or the turn has changed",
        status_code=409,
    )
