"""«Проект кодекса» и «Неточность в статье» (spec 0072 §6, варианты (б)).

Кодекс — образец `conftest.py` (шапка «Статус: Проект»): статьи 1, 2, 2-1 и 3.
"""

from __future__ import annotations

import uuid

from hospitality.modules.mentor.api import (
    ExamAttemptRead,
    QuizGrade,
    article_flag_counts,
    assigned_exam_results,
    codex_is_draft,
    create_exam_assignment,
    my_article_flags,
    record_exam_answer,
    replace_codex,
    set_article_flag,
    set_exam_question,
    start_assigned_exam,
    start_exam,
)
from hospitality.modules.mentor.codex import is_draft_status
from hospitality.modules.mentor.tests.conftest import BODY, HEAD, codex_files, make_user
from hospitality.shared.tenancy import tenant_context

APPROVED = "Название: Кодекс отеля «Тест»\nСтатус: Утверждён 01.12.2026\n"


async def _codex(body: str = BODY, head: str = HEAD) -> None:
    await replace_codex(
        codex_files(body, head), embeddings=None, embedding_model=None, uploaded_by_user_id=None
    )


def test_draft_status_is_the_word_project_at_the_start() -> None:
    assert is_draft_status({"Статус": "Проект"})
    assert is_draft_status({"Статус": " проект от 01.11.2026"})
    assert not is_draft_status({"Статус": "Утверждён 01.12.2026"})
    assert not is_draft_status({"Статус": "Проектный офис"})
    assert not is_draft_status({"Название": "Кодекс"})


async def test_attempt_remembers_draft_codex_at_start(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Снимок при начале: утверждённая редакция прошлую попытку «на проекте» не переписывает."""
    tenant_a, _ = two_tenants
    user = await make_user("Айгерим")
    with tenant_context(tenant_a):
        assert not await codex_is_draft()  # кодекса ещё нет
        await _codex()
        assert await codex_is_draft()
        own = await start_exam(user, chapter_number=None, question_count=1, pass_percent=80)
        assignment = await create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=None
        )
        assigned = await start_assigned_exam(
            user, assignment.id, service_keys=[], question_count=1, pass_percent=80
        )
        assert own.on_draft_codex and assigned.on_draft_codex
        await _codex(head=APPROVED)
        assert not await codex_is_draft()
        after = await start_exam(user, chapter_number=None, question_count=1, pass_percent=80)
        assert not after.on_draft_codex


async def _take(user: uuid.UUID, assignment_id: uuid.UUID, grade: QuizGrade) -> ExamAttemptRead:
    """Назначенный экзамен из одного вопроса с порогом 80 % — до итога."""
    attempt = await start_assigned_exam(
        user, assignment_id, service_keys=[], question_count=1, pass_percent=80
    )
    asked = await set_exam_question(
        user, attempt.id, turn=attempt.turn, article_number="3", question="Что вы сделаете?"
    )
    return await record_exam_answer(
        user, attempt.id, turn=asked.turn, article_number="3", grade=grade
    )


async def test_attempt_on_draft_is_trial(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Вариант (в): сданная на проекте — пробная, в итог по назначению не засчитывается; она
    в итоге, пока зачётных нет, а зачётная — даже несданная — её сменяет и пробной не уступает."""
    tenant_a, _ = two_tenants
    user = await make_user("Айгерим")
    with tenant_context(tenant_a):
        await _codex()
        assignment = await create_exam_assignment(
            chapter_numbers=None, service_keys=[], created_by_user_id=None
        )
        key = (assignment.id, user)
        trial = await _take(user, assignment.id, QuizGrade.CORRECT)
        assert trial.passed and not trial.passes_assignment
        held = (await assigned_exam_results(assignment_ids=[assignment.id], user_ids=[user]))[key]
        assert held.id == trial.id and not held.passes_assignment

        await _codex(head=APPROVED)
        failed = await _take(user, assignment.id, QuizGrade.WRONG)
        await _codex()  # снова проект: новая пробная зачётную несданную не сменяет
        await _take(user, assignment.id, QuizGrade.CORRECT)
        held = (await assigned_exam_results(assignment_ids=[assignment.id], user_ids=[user]))[key]
        assert held.id == failed.id and not held.passes_assignment

        await _codex(head=APPROVED)
        passed = await _take(user, assignment.id, QuizGrade.CORRECT)
        held = (await assigned_exam_results(assignment_ids=[assignment.id], user_ids=[user]))[key]
        assert held.id == passed.id and held.passes_assignment


async def test_flag_is_own_idempotent_and_undoable(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    first, second = await make_user("Первый"), await make_user("Второй")
    with tenant_context(tenant_a):
        await _codex()
        assert await set_article_flag(first, "3", flagged=True)
        assert await set_article_flag(first, "3", flagged=True)  # повтор — одна отметка
        assert await set_article_flag(second, "3", flagged=True)
        assert await set_article_flag(second, "2-1", flagged=True)
        assert await my_article_flags(first) == {"3"}
        counts = await article_flag_counts(user_ids=[first, second])
        assert [(c.article_number, c.title, c.count) for c in counts] == [
            ("3", "Третья", 2),
            ("2-1", "Вставка", 1),
        ]
        assert not await set_article_flag(first, "3", flagged=False)
        assert not await set_article_flag(first, "3", flagged=False)  # снять снятое — ничего
        assert await my_article_flags(first) == set()
        assert [c.count for c in await article_flag_counts(user_ids=[first, second])] == [1, 1]


async def test_flag_of_missing_article_is_not_written(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    user = await make_user("Сотрудник")
    with tenant_context(tenant_a):
        await _codex()
        assert not await set_article_flag(user, "99", flagged=True)
        assert await my_article_flags(user) == set()


async def test_counts_only_listed_accounts(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    """Стёртая учётка в «Сотрудниках» не видна — кабинет её не передаёт, и счёт её не берёт."""
    tenant_a, _ = two_tenants
    shown, erased = await make_user("Видна"), await make_user("Стёрта")
    with tenant_context(tenant_a):
        await _codex()
        await set_article_flag(shown, "1", flagged=True)
        await set_article_flag(erased, "1", flagged=True)
        await set_article_flag(erased, "2", flagged=True)
        counts = await article_flag_counts(user_ids=[shown])
        assert [(c.article_number, c.count) for c in counts] == [("1", 1)]
        assert await article_flag_counts(user_ids=[]) == []


async def test_new_edition_drops_flags_of_changed_articles_only(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """Исправили статью 3 и убрали 2-1 — их отметки ушли; у нетронутой статьи 1 — остались."""
    tenant_a, _ = two_tenants
    user = await make_user("Сотрудник")
    with tenant_context(tenant_a):
        await _codex()
        for number in ("1", "2-1", "3"):
            await set_article_flag(user, number, flagged=True)
        await _codex(head=APPROVED)  # сменилась только шапка — статьи те же
        assert await my_article_flags(user) == {"1", "2-1", "3"}
        fixed = BODY.replace("Источник: стандарт портье.", "Источник: стандарт портье, ред. 2.")
        without_insert = fixed.replace(
            fixed[fixed.index("#### Статья 2-1.") : fixed.index("### Глава 3.")], ""
        )
        await _codex(without_insert)
        assert await my_article_flags(user) == {"1"}


async def test_flags_are_isolated_between_hotels(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    """RLS: отметка отеля A в отеле B не видна и не считается."""
    tenant_a, tenant_b = two_tenants
    user = await make_user("Сотрудник")
    with tenant_context(tenant_a):
        await _codex()
        await set_article_flag(user, "3", flagged=True)
    with tenant_context(tenant_b):
        await _codex()
        assert await my_article_flags(user) == set()
        assert await article_flag_counts(user_ids=[user]) == []
        assert not await set_article_flag(user, "3", flagged=False)
    with tenant_context(tenant_a):
        assert await my_article_flags(user) == {"3"}
