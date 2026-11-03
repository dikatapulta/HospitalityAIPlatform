"""Экзамен — попытка, счёт, итог и изоляция (spec 0048 §12).

Вопросы и проверки здесь задаются готовыми строками: модель — забота `ai/mentor`
(`ai/mentor/tests/test_exam.py`), модуль хранит только ход и счёт.
"""

from __future__ import annotations

import uuid

import pytest

from hospitality.modules.mentor.api import (
    ERR_MENTOR_CODEX_MISSING,
    ERR_MENTOR_NO_EXAM_QUESTION,
    ExamAttemptRead,
    QuizGrade,
    current_exam,
    drop_exam_question,
    exam_percent,
    latest_exam_results,
    list_exam_attempts,
    pick_exam_article,
    record_exam_answer,
    replace_codex,
    set_exam_question,
    start_exam,
)
from hospitality.modules.mentor.tests.conftest import codex_files, make_user
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


async def _codex() -> None:
    await replace_codex(
        codex_files(), embeddings=None, embedding_model=None, uploaded_by_user_id=None
    )


async def _answer(
    user_id: uuid.UUID, attempt: ExamAttemptRead, grade: QuizGrade | None
) -> ExamAttemptRead:
    """Задать вопрос по статье 3 и засчитать ответ с оценкой `grade`."""
    asked = await set_exam_question(
        user_id, attempt.id, turn=attempt.turn, article_number="3", question="Что вы сделаете?"
    )
    return await record_exam_answer(
        user_id, attempt.id, turn=asked.turn, article_number="3", grade=grade
    )


@pytest.mark.parametrize(
    ("correct", "partial", "count", "percent"),
    [
        (7, 2, 10, 80),  # 8 баллов из 10
        (7, 0, 10, 70),  # ровно порог
        (2, 0, 3, 66),  # 66,7 % — вниз, а не «67»
        (6, 1, 10, 65),
        (4, 1, 7, 64),  # 4,5 из 7 = 64,3 %
        (0, 0, 5, 0),
        (5, 0, 5, 100),
    ],
)
def test_percent_is_floored_half_points(
    correct: int, partial: int, count: int, percent: int
) -> None:
    assert exam_percent(correct=correct, partial=partial, question_count=count) == percent


async def test_attempt_runs_to_result_with_threshold_snapshot(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        attempt = await start_exam(anna, chapter_number=3, question_count=2, pass_percent=70)
        assert (attempt.chapter_number, attempt.chapter_title) == (3, "Портье")
        assert await current_exam(anna) == attempt

        attempt = await _answer(anna, attempt, QuizGrade.CORRECT)
        assert (attempt.answered, attempt.finished, attempt.current_question) == (1, False, None)
        attempt = await _answer(anna, attempt, QuizGrade.PARTIAL)
        assert (attempt.percent, attempt.passed, attempt.finished) == (75, True, True)
        assert await current_exam(anna) is None

        # Порог отеля подняли — прошлый итог остаётся со своим порогом.
        stricter = await start_exam(anna, chapter_number=None, question_count=2, pass_percent=80)
        stricter = await _answer(anna, stricter, QuizGrade.CORRECT)
        stricter = await _answer(anna, stricter, QuizGrade.PARTIAL)
        assert (stricter.percent, stricter.passed, stricter.chapter_title) == (75, False, None)

        results = await list_exam_attempts(anna)
        assert [(a.id, a.pass_percent, a.passed) for a in results] == [
            (stricter.id, 80, False),
            (attempt.id, 70, True),
        ]


async def test_ungraded_answer_is_not_counted(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        attempt = await start_exam(anna, chapter_number=None, question_count=1, pass_percent=70)
        attempt = await _answer(anna, attempt, None)
        assert (attempt.answered, attempt.ungraded_count, attempt.turn) == (0, 1, 1)
        assert not attempt.finished  # вместо вопроса — другой
        attempt = await _answer(anna, attempt, QuizGrade.WRONG)
        assert (attempt.percent, attempt.passed) == (0, False)


async def test_new_attempt_abandons_the_running_one(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        first = await start_exam(anna, chapter_number=None, question_count=2, pass_percent=70)
        first = await set_exam_question(
            anna, first.id, turn=0, article_number="3", question="Первый вопрос"
        )
        second = await start_exam(anna, chapter_number=None, question_count=2, pass_percent=70)
        assert await current_exam(anna) == second

        with pytest.raises(AppError) as error:  # ответ из старой вкладки
            await record_exam_answer(
                anna, first.id, turn=1, article_number="3", grade=QuizGrade.CORRECT
            )
        assert error.value.code == ERR_MENTOR_NO_EXAM_QUESTION
        assert await list_exam_attempts(anna) == []  # брошенная в итоги не идёт


async def test_turn_must_match_and_question_must_be_open(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        await _codex()
        attempt = await start_exam(anna, chapter_number=None, question_count=3, pass_percent=70)
        with pytest.raises(AppError):  # вопроса ещё нет
            await record_exam_answer(
                anna, attempt.id, turn=0, article_number="3", grade=QuizGrade.CORRECT
            )
        asked = await set_exam_question(
            anna, attempt.id, turn=0, article_number="3", question="Вопрос"
        )
        with pytest.raises(AppError):  # вопрос уже открыт — второй не задаётся
            await set_exam_question(anna, attempt.id, turn=1, article_number="2-1", question="Ещё")
        with pytest.raises(AppError):  # ответ к другому ходу
            await record_exam_answer(
                anna, attempt.id, turn=0, article_number="3", grade=QuizGrade.CORRECT
            )

        await drop_exam_question(anna, attempt.id, turn=asked.turn)  # статьи нет в кодексе
        dropped = await current_exam(anna)
        assert dropped is not None
        assert (dropped.current_question, dropped.answered, dropped.turn) == (None, 0, 1)


async def test_exam_needs_codex_and_falls_back_to_whole_codex(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    anna = await make_user("Анна")
    with tenant_context(tenant_a):
        with pytest.raises(AppError) as error:
            await start_exam(anna, chapter_number=None, question_count=10, pass_percent=70)
        assert error.value.code == ERR_MENTOR_CODEX_MISSING

        await _codex()
        attempt = await start_exam(anna, chapter_number=99, question_count=10, pass_percent=70)
        assert (attempt.chapter_number, attempt.chapter_title) == (None, None)


async def test_article_pick_prefers_fresh_long_then_fresh_short(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Глава 2: статья 2-1 длинная, статья 2 короткая (conftest)."""
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        await _codex()
        first = await pick_exam_article(chapter_number=2, exclude=[])
        second = await pick_exam_article(chapter_number=2, exclude=["2-1"])
        repeat = await pick_exam_article(chapter_number=2, exclude=["2-1", "2"])
        assert first is not None and first.number == "2-1"
        assert second is not None and second.number == "2"
        assert repeat is not None and repeat.number in {"2", "2-1"}
        assert await pick_exam_article(chapter_number=99, exclude=[]) is None


async def test_attempts_are_isolated_between_hotels_and_users(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, tenant_b = two_tenants
    anna, boris = await make_user("Анна"), await make_user("Борис")
    with tenant_context(tenant_a):
        await _codex()
        attempt = await start_exam(anna, chapter_number=None, question_count=1, pass_percent=70)
        await _answer(anna, attempt, QuizGrade.CORRECT)
        assert len(await list_exam_attempts(anna)) == 1
        assert await list_exam_attempts(boris) == []
        with pytest.raises(AppError):  # чужая попытка — как не своя
            await set_exam_question(boris, attempt.id, turn=1, article_number="3", question="?")
    with tenant_context(tenant_b):
        assert await list_exam_attempts(anna) == []
        assert await current_exam(anna) is None


async def test_latest_result_is_last_finished_attempt_per_user(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Сводка по службам (#780): итог — последняя законченная, брошенная и идущая — мимо."""
    tenant_a, tenant_b = two_tenants
    anna, boris, vera = await make_user("Анна"), await make_user("Борис"), await make_user("Вера")
    with tenant_context(tenant_a):
        await _codex()
        first = await start_exam(anna, chapter_number=None, question_count=1, pass_percent=70)
        await _answer(anna, first, QuizGrade.WRONG)
        second = await start_exam(anna, chapter_number=None, question_count=1, pass_percent=70)
        await _answer(anna, second, QuizGrade.CORRECT)
        # Новая попытка Анны брошена на полпути — итог остаётся за второй.
        await start_exam(anna, chapter_number=None, question_count=2, pass_percent=70)
        boris_try = await start_exam(boris, chapter_number=None, question_count=1, pass_percent=70)
        await _answer(boris, boris_try, QuizGrade.PARTIAL)
        # Вера начала и не закончила — её в итогах нет.
        await start_exam(vera, chapter_number=None, question_count=3, pass_percent=70)

        results = {
            row.user_id: row for row in await latest_exam_results(user_ids=[anna, boris, vera])
        }
        assert set(results) == {anna, boris}
        assert (results[anna].id, results[anna].percent, results[anna].passed) == (
            second.id,
            100,
            True,
        )
        assert (results[boris].percent, results[boris].passed) == (50, False)
        # Учётки вне `user_ids` (стёртые — их кабинет не передаёт) не видны.
        assert [row.user_id for row in await latest_exam_results(user_ids=[boris])] == [boris]
        assert await latest_exam_results(user_ids=[]) == []
    with tenant_context(tenant_b):
        assert await latest_exam_results(user_ids=[anna, boris]) == []
