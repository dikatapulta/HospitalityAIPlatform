"""Экзамен «Наставника» (spec 0048 §12): попытка, вопросы по очереди, счёт и итог.
Назначенный экзамен и итог по назначению — `assignments.py` (spec 0072 §3).

Вопрос задаёт и ответ проверяет композиция `ai/mentor/exam.py` тем же ходом, что
«Тренировка»; модуль хранит только счёт и открытый вопрос — текстов ответов и
проверок нет. Идущая попытка сотрудника — его последняя начатая, пока она не
закончена: новая попытка бросает прежнюю, та остаётся без итога и в итоги не идёт.

Запись хода сверяет под блокировкой строки попытку и номер хода (`turn` — сколько
вопросов задано), которые видел сотрудник: ответ из второй вкладки или после
«Начать экзамен» в другой вкладке не засчитается не к тому вопросу — ERR-MENTOR-004.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Collection, Sequence
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.mentor.codex import is_draft_status
from hospitality.modules.mentor.models import (
    MentorCodex,
    MentorCodexArticle,
    MentorExamAttempt,
    QuizGrade,
)
from hospitality.modules.mentor.schemas import CodexArticleRead, ExamAttemptRead
from hospitality.modules.mentor.service import (
    ERR_MENTOR_CODEX_MISSING,
    QUIZ_MIN_ARTICLE_CHARS,
    _quiz_candidates,
    has_codex,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_MENTOR_NO_EXAM_QUESTION = "ERR-MENTOR-004"

# Сколько последних законченных попыток видит сотрудник.
EXAM_ATTEMPTS_SHOWN: Final = 20


def exam_percent(*, correct: int, partial: int, question_count: int) -> int:
    """Процент вниз до целого: с целым порогом «процент ≥ порога» — то же, что точный счёт.

    Верно — 1 балл, частично — 0,5, неверно — 0; счёт — в половинах балла, без дробей.
    69,6 % показать как «70 %, не сдан» было бы неправдой, поэтому не округление.
    """
    half_points = 2 * correct + partial
    return half_points * 100 // (2 * question_count)


async def start_exam(
    user_id: uuid.UUID, *, chapter_number: int | None, question_count: int, pass_percent: int
) -> ExamAttemptRead:
    """Экзамен для себя (spec 0072 §3) — новая попытка со снимком настроек; идущая
    прежняя остаётся брошенной. В итоги для менеджера не идёт.

    Кодекса нет — ERR-MENTOR-002. Главы больше нет в кодексе (перезагружен, пока
    страница была открыта) — экзамен по всему кодексу.
    """
    await require_codex()
    async with session_scope() as session:
        chapter_title: str | None = None
        if chapter_number is not None:
            chapter_title = await session.scalar(
                select(MentorCodexArticle.chapter_title)
                .where(MentorCodexArticle.chapter_number == chapter_number)
                .limit(1)
            )
            if chapter_title is None:
                chapter_number = None
        result = await add_attempt(
            session,
            user_id,
            assignment_id=None,
            chapter=None if chapter_number is None else (chapter_number, chapter_title or ""),
            question_count=question_count,
            pass_percent=pass_percent,
        )
    logger.info(
        "mentor_exam_started",
        question_count=question_count,
        pass_percent=pass_percent,
        whole_codex=chapter_number is None,
        assigned=False,
    )
    return result


async def require_codex() -> None:
    """Кодекса нет — ERR-MENTOR-002: экзамена без статей не бывает."""
    if not await has_codex():
        raise AppError(
            code=ERR_MENTOR_CODEX_MISSING,
            message="Codex is not uploaded for this hotel",
            status_code=409,
        )


async def add_attempt(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    assignment_id: uuid.UUID | None,
    chapter: tuple[int, str] | None,
    question_count: int,
    pass_percent: int,
) -> ExamAttemptRead:
    """Строка попытки: тема (`chapter` — номер и снимок названия, `None` — весь кодекс
    или тема назначения), снимок настроек отеля и статуса кодекса (spec 0072 §6)."""
    meta = await session.scalar(select(MentorCodex.meta))
    attempt = MentorExamAttempt(
        user_id=user_id,
        assignment_id=assignment_id,
        chapter_number=None if chapter is None else chapter[0],
        chapter_title=None if chapter is None else chapter[1],
        question_count=question_count,
        pass_percent=pass_percent,
        on_draft_codex=meta is not None and is_draft_status(meta),
        started_at=utc_now(),
    )
    session.add(attempt)
    await session.flush()
    return ExamAttemptRead.model_validate(attempt)


async def pick_exam_article(
    *, chapters: Collection[int] | None, exclude: Sequence[str]
) -> CodexArticleRead | None:
    """Статья следующего вопроса из глав `chapters` (`None` — весь кодекс); `None` —
    в главах статей нет.

    Сначала — не спрошенные в этой попытке от `QUIZ_MIN_ARTICLE_CHARS` знаков, затем
    не спрошенные короткие; спрошены все — повтор (в теме статей меньше, чем вопросов).
    В отличие от «Тренировки» короткая статья лучше повтора: экзамен меряет тему, а
    не одну статью.
    """
    rows = await _quiz_candidates(chapters=chapters, near=None, min_chars=0)
    fresh = [row for row in rows if row.number not in exclude]
    long_fresh = [row for row in fresh if len(row.body) >= QUIZ_MIN_ARTICLE_CHARS]
    pool = long_fresh or fresh or rows
    return random.choice(pool) if pool else None  # noqa: S311 — выбор вопроса, не криптография


async def current_exam(user_id: uuid.UUID) -> ExamAttemptRead | None:
    """Идущая попытка сотрудника; `None` — последняя начатая закончена или попыток нет."""
    async with session_scope() as session:
        attempt = await session.scalar(
            select(MentorExamAttempt)
            .where(MentorExamAttempt.user_id == user_id)
            .order_by(MentorExamAttempt.started_at.desc())
            .limit(1)
        )
        if attempt is None or attempt.finished_at is not None:
            return None
        return ExamAttemptRead.model_validate(attempt)


async def set_exam_question(
    user_id: uuid.UUID, attempt_id: uuid.UUID, *, turn: int, article_number: str, question: str
) -> ExamAttemptRead:
    """Записать заданный вопрос; вопрос уже открыт или попытка сменилась — ERR-MENTOR-004."""
    async with session_scope() as session:
        attempt = await _lock_current(session, user_id, attempt_id, turn=turn)
        if attempt.current_question is not None:
            raise _no_exam_question()
        attempt.current_article_number = article_number
        attempt.current_question = question
        attempt.asked_articles = [*attempt.asked_articles, article_number]
        await session.flush()
        return ExamAttemptRead.model_validate(attempt)


async def record_exam_answer(
    user_id: uuid.UUID,
    attempt_id: uuid.UUID,
    *,
    turn: int,
    article_number: str,
    grade: QuizGrade | None,
) -> ExamAttemptRead:
    """Засчитать проверенный ответ и, на последнем вопросе, подвести итог.

    Оценка не разобрана (`None`) — ответ не засчитан, вместо вопроса будет другой:
    молча в ноль он не идёт (spec 0048 §12). Вопрос уже не открыт или сменился —
    ERR-MENTOR-004.
    """
    async with session_scope() as session:
        attempt = await _lock_current(session, user_id, attempt_id, turn=turn)
        if attempt.current_article_number != article_number or attempt.current_question is None:
            raise _no_exam_question()
        attempt.current_article_number = None
        attempt.current_question = None
        if grade is None:
            attempt.ungraded_count += 1
        elif grade is QuizGrade.CORRECT:
            attempt.correct_count += 1
        elif grade is QuizGrade.PARTIAL:
            attempt.partial_count += 1
        else:
            attempt.wrong_count += 1
        answered = attempt.correct_count + attempt.partial_count + attempt.wrong_count
        if answered >= attempt.question_count:
            attempt.percent = exam_percent(
                correct=attempt.correct_count,
                partial=attempt.partial_count,
                question_count=attempt.question_count,
            )
            attempt.passed = attempt.percent >= attempt.pass_percent
            attempt.finished_at = utc_now()
        await session.flush()
        result = ExamAttemptRead.model_validate(attempt)
    logger.info("mentor_exam_answered", turn=turn, grade=grade and grade.value)
    if result.finished:
        logger.info("mentor_exam_finished", percent=result.percent, passed=result.passed)
    return result


async def drop_exam_question(user_id: uuid.UUID, attempt_id: uuid.UUID, *, turn: int) -> None:
    """Снять открытый вопрос без счёта — его статьи нет в перезагруженном кодексе.

    Попытка или ход сменились — ничего не делает: снимать уже нечего.
    """
    async with session_scope() as session:
        try:
            attempt = await _lock_current(session, user_id, attempt_id, turn=turn)
        except AppError:
            return
        attempt.current_article_number = None
        attempt.current_question = None
    logger.warning("mentor_exam_question_dropped", reason="article_gone")


async def list_exam_attempts(
    user_id: uuid.UUID, *, limit: int = EXAM_ATTEMPTS_SHOWN
) -> list[ExamAttemptRead]:
    """Законченные попытки сотрудника, свежие сверху; брошенные сюда не идут."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorExamAttempt)
            .where(
                MentorExamAttempt.user_id == user_id,
                MentorExamAttempt.finished_at.is_not(None),
            )
            .order_by(MentorExamAttempt.finished_at.desc())
            .limit(limit)
        )
        return [ExamAttemptRead.model_validate(row) for row in rows]


async def _lock_current(
    session: AsyncSession, user_id: uuid.UUID, attempt_id: uuid.UUID, *, turn: int
) -> MentorExamAttempt:
    """Попытка под блокировкой — идущая, последняя начатая и на том же ходу; иначе отказ."""
    attempt = await session.scalar(
        select(MentorExamAttempt)
        .where(MentorExamAttempt.id == attempt_id, MentorExamAttempt.user_id == user_id)
        .with_for_update()
    )
    if attempt is None or attempt.finished_at is not None or len(attempt.asked_articles) != turn:
        raise _no_exam_question()
    newer = await session.scalar(
        select(MentorExamAttempt.id)
        .where(
            MentorExamAttempt.user_id == user_id,
            MentorExamAttempt.started_at > attempt.started_at,
        )
        .limit(1)
    )
    if newer is not None:
        raise _no_exam_question()
    return attempt


def _no_exam_question() -> AppError:
    return AppError(
        code=ERR_MENTOR_NO_EXAM_QUESTION,
        message="Exam question is no longer open: the attempt or the turn has changed",
        status_code=409,
    )
