"""Назначенный экзамен «Наставника» (spec 0072 §3): кому и по какой теме, итог по назначению.

Менеджер назначает тему — весь кодекс или главы — всем или службам; сотрудник сдаёт её тем
же ходом, что экзамен для себя (`exams.py`), а «Итоги экзаменов» считают только назначенный.
Главы назначения хранятся снимком «номер + название» и ищутся в нынешнем кодексе по обоим:
перезагрузка кодекса могла сдвинуть нумерацию, и глава «12. Горничная» не должна молча
стать главой 12 о другом. Ненайденные пропускаются; не нашлось ни одной — ERR-MENTOR-006.

Назначение не правится: «Снять» — `archived_at`. Снятое пропадает у сотрудников и из итогов,
попытки остаются; начатая попытка доходит до итога (ход её назначение не перечитывает).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.mentor.exams import add_attempt, require_codex
from hospitality.modules.mentor.models import (
    MentorCodexArticle,
    MentorExamAssignment,
    MentorExamAttempt,
)
from hospitality.modules.mentor.schemas import ExamAssignmentRead, ExamAttemptRead, ExamChapterRef
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_MENTOR_EXAM_UNAVAILABLE = "ERR-MENTOR-006"


async def create_exam_assignment(
    *,
    chapter_numbers: Collection[int] | None,
    service_keys: Collection[str],
    created_by_user_id: uuid.UUID | None,
) -> ExamAssignmentRead:
    """Назначить экзамен: `chapter_numbers=None` — весь кодекс, `service_keys` пусто — всем.

    Главы — снимок номера и названия из нынешнего кодекса, по номеру; номеров, которых в
    кодексе нет (страница открыта до перезагрузки), не берёт, а не нашлось ни одного —
    ERR-MENTOR-006. Кодекса нет — ERR-MENTOR-002. Ключи служб кабинет сверил с категориями
    отеля до вызова (ERR-AUTH-015).
    """
    if chapter_numbers is not None and not chapter_numbers:
        raise ValueError("create_exam_assignment: пустой список глав — это «весь кодекс» (None)")
    await require_codex()
    async with session_scope() as session:
        chapters: list[ExamChapterRef] = []
        if chapter_numbers is not None:
            codex = await _codex_chapters(session)
            chapters = [
                ExamChapterRef(number=number, title=codex[number])
                for number in sorted(set(chapter_numbers))
                if number in codex
            ]
            if not chapters:
                raise _unavailable("None of the chapters is in the current codex")
        assignment = MentorExamAssignment(
            chapters=[chapter.model_dump() for chapter in chapters],
            service_keys=sorted(set(service_keys)),
            created_by_user_id=created_by_user_id,
            created_at=utc_now(),
        )
        session.add(assignment)
        await session.flush()
        result = ExamAssignmentRead.model_validate(assignment)
    logger.info(
        "mentor_exam_assigned",
        chapter_count=len(result.chapters),
        service_count=len(result.service_keys),
    )
    return result


async def archive_exam_assignment(assignment_id: uuid.UUID) -> bool:
    """«Снять»: `False` — назначения нет или оно уже снято (повтор ничего не меняет)."""
    async with session_scope() as session:
        assignment = await session.get(MentorExamAssignment, assignment_id, with_for_update=True)
        if assignment is None or assignment.archived_at is not None:
            return False
        assignment.archived_at = utc_now()
    logger.info("mentor_exam_assignment_archived")
    return True


async def list_exam_assignments() -> list[ExamAssignmentRead]:
    """Действующие назначения отеля, свежие сверху."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorExamAssignment)
            .where(MentorExamAssignment.archived_at.is_(None))
            .order_by(MentorExamAssignment.created_at.desc())
        )
        return [ExamAssignmentRead.model_validate(row) for row in rows]


async def get_exam_assignments(
    assignment_ids: Collection[uuid.UUID],
) -> dict[uuid.UUID, ExamAssignmentRead]:
    """Назначения по id, снятые тоже — тема прошлых попыток в «Моих попытках»."""
    if not assignment_ids:
        return {}
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorExamAssignment).where(MentorExamAssignment.id.in_(assignment_ids))
        )
        return {row.id: ExamAssignmentRead.model_validate(row) for row in rows}


async def start_assigned_exam(
    user_id: uuid.UUID,
    assignment_id: uuid.UUID,
    *,
    service_keys: Collection[str],
    question_count: int,
    pass_percent: int,
) -> ExamAttemptRead:
    """Попытка назначенного экзамена; идущая прежняя остаётся брошенной.

    `service_keys` — нынешние службы сотрудника (spec 0045). Назначение снято, не для его
    служб или ни одной его главы нет в нынешнем кодексе — ERR-MENTOR-006, попытки нет.
    Тема попытки — единственная найденная глава; глав несколько или весь кодекс — `NULL`,
    тему держит назначение (`exam_attempt_chapters`).
    """
    await require_codex()
    async with session_scope() as session:
        row = await session.get(MentorExamAssignment, assignment_id)
        assignment = None if row is None else ExamAssignmentRead.model_validate(row)
        if assignment is None or assignment.archived_at is not None:
            raise _unavailable("Exam assignment is archived or missing")
        if not assignment.is_for(service_keys):
            raise _unavailable("Exam assignment is not for the employee's services")
        chapter: tuple[int, str] | None = None
        if assignment.chapters:
            codex = await _codex_chapters(session)
            found = _found(assignment.chapters, codex)
            if not found:
                raise _unavailable("None of the assignment chapters is in the current codex")
            if len(found) == 1:
                chapter = (found[0], codex[found[0]])
        result = await add_attempt(
            session,
            user_id,
            assignment_id=assignment.id,
            chapter=chapter,
            question_count=question_count,
            pass_percent=pass_percent,
        )
    logger.info(
        "mentor_exam_started",
        question_count=question_count,
        pass_percent=pass_percent,
        whole_codex=not assignment.chapters,
        assigned=True,
    )
    return result


async def exam_attempt_chapters(attempt: ExamAttemptRead) -> list[int] | None:
    """Главы, из которых берётся следующий вопрос попытки; `None` — весь кодекс.

    Экзамен для себя — его глава. Назначенный — главы назначения, найденные в нынешнем
    кодексе (снятое назначение тоже: начатая попытка доходит до итога); кодекс
    перезагрузили посреди попытки и не нашлось ни одной — весь кодекс, как у экзамена
    для себя, чья глава пропала.
    """
    if attempt.assignment_id is None:
        return None if attempt.chapter_number is None else [attempt.chapter_number]
    async with session_scope() as session:
        row = await session.get(MentorExamAssignment, attempt.assignment_id)
        if row is None or not row.chapters:
            return None
        assignment = ExamAssignmentRead.model_validate(row)
        found = _found(assignment.chapters, await _codex_chapters(session))
    return found or None


async def assigned_exam_results(
    *, assignment_ids: Collection[uuid.UUID], user_ids: Collection[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], ExamAttemptRead]:
    """Итог по назначению — ключ `(назначение, сотрудник)` (spec 0072 §3).

    Сдан однажды — сдан: первая сданная попытка (её дата), пересдача результат не
    отнимает; не сдавал — последняя законченная; не заканчивал ни одной — ключа нет.
    Пробная попытка — начатая на проекте кодекса (`on_draft_codex`, spec 0072 §6, вариант (в))
    — не засчитывается: она в итоге, только пока зачётных нет (последняя пробная), и
    `passes_assignment` у неё ложно. Брошенные и идущие попытки и экзамен для себя не идут.
    Учётки передаёт кабинет — стёртых модуль не знает.
    """
    if not assignment_ids or not user_ids:
        return {}
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorExamAttempt)
            .where(
                MentorExamAttempt.assignment_id.in_(assignment_ids),
                MentorExamAttempt.user_id.in_(user_ids),
                MentorExamAttempt.finished_at.is_not(None),
            )
            .order_by(MentorExamAttempt.finished_at)
        )
        results: dict[tuple[uuid.UUID, uuid.UUID], ExamAttemptRead] = {}
        for row in rows:
            if row.assignment_id is None:  # pragma: no cover — отобрано условием выше
                continue
            key = (row.assignment_id, row.user_id)
            held = results.get(key)
            if held is None or _replaces(held, on_draft=row.on_draft_codex):
                results[key] = ExamAttemptRead.model_validate(row)
        return results


async def ungraded_exam_answers(
    *, assignment_ids: Collection[uuid.UUID], user_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Не засчитанные ответы по назначению — оценку модели не удалось разобрать (#1050).

    Сумма по всем попыткам сотрудников из списка, законченным и нет: попытка, где не
    разбирается ни одна оценка, не закончится никогда, и в итогах сотрудник числился бы
    «нет попыток». Назначения без таких ответов в словаре нет.
    """
    if not assignment_ids or not user_ids:
        return {}
    async with session_scope() as session:
        rows = await session.execute(
            select(MentorExamAttempt.assignment_id, func.sum(MentorExamAttempt.ungraded_count))
            .where(
                MentorExamAttempt.assignment_id.in_(assignment_ids),
                MentorExamAttempt.user_id.in_(user_ids),
                MentorExamAttempt.ungraded_count > 0,
            )
            .group_by(MentorExamAttempt.assignment_id)
        )
        return {row[0]: int(row[1]) for row in rows if row[0] is not None}


def _replaces(held: ExamAttemptRead, *, on_draft: bool) -> bool:
    """Сменяет ли следующая по времени попытка итог: пробную — любая, зачётную несданную —
    только зачётная, сданную зачётную — никакая."""
    if held.on_draft_codex:
        return True
    return not held.passed and not on_draft


async def _codex_chapters(session: AsyncSession) -> dict[int, str]:
    """Главы нынешнего кодекса: номер → название."""
    rows = await session.execute(
        select(MentorCodexArticle.chapter_number, MentorCodexArticle.chapter_title).distinct()
    )
    return {number: title for number, title in rows.tuples()}


def _found(chapters: Collection[ExamChapterRef], codex: dict[int, str]) -> list[int]:
    """Номера глав снимка, которые в кодексе есть с тем же номером и тем же названием."""
    return [chapter.number for chapter in chapters if codex.get(chapter.number) == chapter.title]


def _unavailable(message: str) -> AppError:
    return AppError(code=ERR_MENTOR_EXAM_UNAVAILABLE, message=message, status_code=409)
