"""Назначенный экзамен — кому, тема, итог по назначению, снятие, изоляция (spec 0072 §3).

Кодекс — образец `conftest.py`: главы 1 «О кодексе», 2 «Консьерж», 3 «Портье». Вопросы
и проверки — готовыми строками, как в `test_exams.py`: модель — забота `ai/mentor`.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.mentor.api import (
    ERR_MENTOR_CODEX_MISSING,
    ERR_MENTOR_EXAM_UNAVAILABLE,
    ExamAttemptRead,
    ExamChapterRef,
    QuizGrade,
    archive_exam_assignment,
    assigned_exam_results,
    create_exam_assignment,
    exam_attempt_chapters,
    get_exam_assignments,
    list_exam_assignments,
    record_exam_answer,
    replace_codex,
    set_exam_question,
    start_assigned_exam,
    start_exam,
)
from hospitality.modules.mentor.tests.conftest import BODY, codex_files, make_user
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

HOUSEKEEPING = ["housekeeping"]


# Утверждённая редакция: попытка на проекте — пробная и в итог не идёт (spec 0072 §6,
# вариант (в); проверяет `test_flags.py`), а здесь — итог зачётных.
APPROVED = "Название: Кодекс отеля «Тест»\nСтатус: Утверждён\n"


async def _codex(body: str = BODY) -> None:
    await replace_codex(
        codex_files(body, APPROVED),
        embeddings=None,
        embedding_model=None,
        uploaded_by_user_id=None,
    )


async def _finish(user_id: uuid.UUID, attempt: ExamAttemptRead, *grades: QuizGrade) -> None:
    """Довести попытку до итога оценками `grades` (по одной на вопрос)."""
    for grade in grades:
        asked = await set_exam_question(
            user_id, attempt.id, turn=attempt.turn, article_number="3", question="Что вы сделаете?"
        )
        attempt = await record_exam_answer(
            user_id, attempt.id, turn=asked.turn, article_number="3", grade=grade
        )
    assert attempt.finished


async def _take(
    user_id: uuid.UUID, assignment_id: uuid.UUID, grade: QuizGrade, *, services: list[str]
) -> ExamAttemptRead:
    """Назначенный экзамен из одного вопроса с порогом 70 %."""
    attempt = await start_assigned_exam(
        user_id, assignment_id, service_keys=services, question_count=1, pass_percent=70
    )
    await _finish(user_id, attempt, grade)
    return attempt


def _unavailable(error: pytest.ExceptionInfo[AppError]) -> bool:
    return error.value.code == ERR_MENTOR_EXAM_UNAVAILABLE and error.value.status_code == 409


async def test_assignment_snapshots_chapters_and_needs_codex(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    manager = await make_user("Менеджер")
    with tenant_context(tenant_a):
        with pytest.raises(AppError) as missing:
            await create_exam_assignment(
                chapter_numbers=None, service_keys=[], created_by_user_id=manager
            )
        assert missing.value.code == ERR_MENTOR_CODEX_MISSING
        await _codex()
        with pytest.raises(ValueError, match="весь кодекс"):
            await create_exam_assignment(
                chapter_numbers=[], service_keys=[], created_by_user_id=manager
            )
        # Номера нет в кодексе (страница открыта до перезагрузки) — не берётся.
        chosen = await create_exam_assignment(
            chapter_numbers=[3, 99, 2, 3],
            service_keys=["reception", "housekeeping", "reception"],
            created_by_user_id=manager,
        )
        assert chosen.chapters == [
            ExamChapterRef(number=2, title="Консьерж"),
            ExamChapterRef(number=3, title="Портье"),
        ]
        assert chosen.service_keys == ["housekeeping", "reception"]
        with pytest.raises(AppError) as none_found:
            await create_exam_assignment(
                chapter_numbers=[99], service_keys=[], created_by_user_id=manager
            )
        assert _unavailable(none_found)
        whole = await create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=manager
        )
        assert (whole.chapters, whole.service_keys) == ([], [])
        assert [a.id for a in await list_exam_assignments()] == [whole.id, chosen.id]


async def test_assignment_is_for_all_or_its_services_only(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        everyone = await create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=None
        )
        rooms = await create_exam_assignment(
            chapter_numbers=[3], service_keys=HOUSEKEEPING, created_by_user_id=None
        )
        assert everyone.is_for([]) and everyone.is_for(["reception"])
        assert rooms.is_for(["reception", "housekeeping"]) and not rooms.is_for(["reception"])
        with pytest.raises(AppError) as foreign:
            await start_assigned_exam(
                anna, rooms.id, service_keys=["reception"], question_count=1, pass_percent=70
            )
        assert _unavailable(foreign)
        attempt = await start_assigned_exam(
            anna, rooms.id, service_keys=HOUSEKEEPING, question_count=2, pass_percent=80
        )
        assert (attempt.assignment_id, attempt.chapter_number, attempt.chapter_title) == (
            rooms.id,
            3,
            "Портье",
        )
        assert (attempt.question_count, attempt.pass_percent) == (2, 80)


async def test_assignment_to_two_services_is_for_staff_of_either(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Назначен двум службам — сотрудник одной из них его видит и сдаёт (#1052):
    в остальных тестах назначение всегда одной службе."""
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        both = await create_exam_assignment(
            chapter_numbers=None,
            service_keys=["housekeeping", "reception"],
            created_by_user_id=None,
        )
        assert both.is_for(HOUSEKEEPING) and both.is_for(["reception"])
        assert not both.is_for(["concierge"])
        attempt = await start_assigned_exam(
            anna, both.id, service_keys=HOUSEKEEPING, question_count=1, pass_percent=70
        )
        assert attempt.assignment_id == both.id


async def test_passed_once_stays_passed_and_own_exam_does_not_count(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna, boris, vera, gleb = (
        await make_user("Анна"),
        await make_user("Борис"),
        await make_user("Вера"),
        await make_user("Глеб"),
    )
    with tenant_context(tenant_a):
        await _codex()
        exam = await create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=None
        )
        other = await create_exam_assignment(
            chapter_numbers=[2], service_keys=[], created_by_user_id=None
        )
        await _take(anna, exam.id, QuizGrade.WRONG, services=[])
        passed = await _take(anna, exam.id, QuizGrade.CORRECT, services=[])
        await _take(anna, exam.id, QuizGrade.WRONG, services=[])  # пересдача не отнимает
        await _take(boris, exam.id, QuizGrade.WRONG, services=[])
        last = await _take(boris, exam.id, QuizGrade.PARTIAL, services=[])
        await _take(boris, other.id, QuizGrade.CORRECT, services=[])  # другое назначение
        # Вера сдала только экзамен для себя, Глеб бросил назначенный — оба без итога.
        own = await start_exam(vera, chapter_number=None, question_count=1, pass_percent=70)
        await _finish(vera, own, QuizGrade.CORRECT)
        await start_assigned_exam(gleb, exam.id, service_keys=[], question_count=3, pass_percent=70)

        results = await assigned_exam_results(
            assignment_ids=[exam.id], user_ids=[anna, boris, vera, gleb]
        )
        assert set(results) == {(exam.id, anna), (exam.id, boris)}
        assert (results[exam.id, anna].id, results[exam.id, anna].passed) == (passed.id, True)
        assert (results[exam.id, boris].id, results[exam.id, boris].percent) == (last.id, 50)
        both = await assigned_exam_results(assignment_ids=[exam.id, other.id], user_ids=[boris])
        assert both[other.id, boris].passed is True
        assert await assigned_exam_results(assignment_ids=[], user_ids=[anna]) == {}
        assert await assigned_exam_results(assignment_ids=[exam.id], user_ids=[]) == {}


async def test_archived_assignment_cannot_start_but_running_attempt_finishes(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        exam = await create_exam_assignment(
            chapter_numbers=[3], service_keys=[], created_by_user_id=None
        )
        running = await start_assigned_exam(
            anna, exam.id, service_keys=[], question_count=1, pass_percent=70
        )
        assert await archive_exam_assignment(exam.id) is True
        assert await archive_exam_assignment(exam.id) is False  # повтор ничего не меняет
        assert await archive_exam_assignment(uuid.uuid4()) is False
        assert await list_exam_assignments() == []
        with pytest.raises(AppError) as archived:
            await start_assigned_exam(
                anna, exam.id, service_keys=[], question_count=1, pass_percent=70
            )
        assert _unavailable(archived)
        # Начатая попытка доходит до итога, тема — главы снятого назначения.
        assert await exam_attempt_chapters(running) == [3]
        await _finish(anna, running, QuizGrade.CORRECT)
        kept = await get_exam_assignments([exam.id])
        assert kept[exam.id].archived_at is not None  # тема прошлых попыток остаётся


async def test_chapter_found_by_number_and_title_after_codex_reload(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        pair = await create_exam_assignment(
            chapter_numbers=[2, 3], service_keys=[], created_by_user_id=None
        )
        porter = await create_exam_assignment(
            chapter_numbers=[3], service_keys=[], created_by_user_id=None
        )
        whole = await create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=None
        )
        both = await start_assigned_exam(
            anna, pair.id, service_keys=[], question_count=1, pass_percent=70
        )
        assert (both.chapter_number, await exam_attempt_chapters(both)) == (None, [2, 3])

        # Глава 3 переименована перезагрузкой: тот же номер — уже другая глава.
        await _codex(BODY.replace("Глава 3. Портье", "Глава 3. Швейцар"))
        assert await exam_attempt_chapters(both) == [2]
        narrowed = await start_assigned_exam(
            anna, pair.id, service_keys=[], question_count=1, pass_percent=70
        )
        assert (narrowed.chapter_number, narrowed.chapter_title) == (2, "Консьерж")
        with pytest.raises(AppError) as gone:
            await start_assigned_exam(
                anna, porter.id, service_keys=[], question_count=1, pass_percent=70
            )
        assert _unavailable(gone)
        attempt = await start_assigned_exam(
            anna, whole.id, service_keys=[], question_count=1, pass_percent=70
        )
        assert await exam_attempt_chapters(attempt) is None
        own = await start_exam(anna, chapter_number=2, question_count=1, pass_percent=70)
        assert await exam_attempt_chapters(own) == [2]


async def test_assignments_are_isolated_between_hotels(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """RLS: назначение отеля A в отеле B не видно, не начинается и не снимается."""
    tenant_a, tenant_b = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        exam = await create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=None
        )
        await _take(anna, exam.id, QuizGrade.CORRECT, services=[])
    with tenant_context(tenant_b):
        await _codex()
        assert await list_exam_assignments() == []
        assert await get_exam_assignments([exam.id]) == {}
        assert await archive_exam_assignment(exam.id) is False
        assert await assigned_exam_results(assignment_ids=[exam.id], user_ids=[anna]) == {}
        with pytest.raises(AppError) as foreign:
            await start_assigned_exam(
                anna, exam.id, service_keys=[], question_count=1, pass_percent=70
            )
        assert _unavailable(foreign)
    with tenant_context(tenant_a):
        assert [a.id for a in await list_exam_assignments()] == [exam.id]
