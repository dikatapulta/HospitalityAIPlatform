"""Экзамен (spec 0048 §12): вопрос и проверка ходом «Тренировки», счёт и итог в `done`."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator

import pytest

from hospitality.ai.gateway.api import MockLlmProvider
from hospitality.ai.mentor.codex_upload import upload_codex
from hospitality.ai.mentor.common import MentorEvent, MentorProviders
from hospitality.ai.mentor.exam import ExamDone, check_exam_answer, next_exam_question
from hospitality.ai.mentor.tests.conftest import CODEX, codex_files, providers
from hospitality.modules.mentor import api as mentor
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

QUESTION = "Гость подходит, пока вы говорите по телефону. Что вы сделаете?"


async def _done(events: AsyncGenerator[MentorEvent, None]) -> ExamDone:
    *_, last = [event async for event in events]
    assert isinstance(last, ExamDone)
    return last


def _calls(fakes: MentorProviders) -> int:
    assert isinstance(fakes.streaming, MockLlmProvider)
    return len(fakes.streaming.calls)


async def _ask(user_id: uuid.UUID, attempt_id: uuid.UUID) -> ExamDone:
    return await _done(
        next_exam_question(user_id, attempt_id, providers=providers(answer=QUESTION))
    )


async def _check(user_id: uuid.UUID, asked: ExamDone, verdict: str) -> ExamDone:
    return await _done(
        check_exam_answer(
            user_id,
            "Закончу звонок и поздороваюсь.",
            attempt_id=asked.exam.attempt_id,
            turn=asked.exam.turn,
            providers=providers(answer=verdict),
        )
    )


@pytest.mark.usefixtures("mentor_enabled")
async def test_exam_runs_question_by_question_to_result(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=providers())
        attempt = await mentor.start_exam(
            user_id, chapter_number=2, question_count=2, pass_percent=70
        )

        asking = providers(answer=QUESTION)
        asked = await _done(next_exam_question(user_id, attempt.id, providers=asking))
        assert (asked.answer, asked.sources) == (QUESTION, [])  # источник выдал бы ответ
        assert (asked.exam.turn, asked.exam.answered, asked.exam.total) == (1, 0, 2)
        assert isinstance(asking.streaming, MockLlmProvider)
        # Глава 2: сначала длинная статья 2, а не короткая 3.
        assert asking.streaming.calls[0].messages[0].content.startswith("[Статья 2]")

        checking = providers(answer="Оценка: верно\nВсё так [Статья 2].")
        checked = await _done(
            check_exam_answer(
                user_id, "Закончу звонок.", attempt_id=attempt.id, turn=1, providers=checking
            )
        )
        assert [s.number for s in checked.sources] == ["2"]
        assert (checked.exam.counted, checked.exam.answered, checked.exam.finished) == (
            True,
            1,
            False,
        )
        assert isinstance(checking.streaming, MockLlmProvider)
        payload = json.loads(checking.streaming.calls[0].messages[0].content)
        assert (payload["вопрос"], payload["ответ_сотрудника"]) == (QUESTION, "Закончу звонок.")

        second = await _ask(user_id, attempt.id)
        assert second.exam.turn == 2
        result = await _check(user_id, second, "Оценка: частично верно\nНе хватило.")
        assert (result.exam.finished, result.exam.percent, result.exam.passed) == (True, 75, True)
        assert (result.exam.correct, result.exam.partial, result.exam.wrong) == (1, 1, 0)
        assert await mentor.current_exam(user_id) is None


@pytest.mark.usefixtures("mentor_enabled")
async def test_ungraded_verdict_is_shown_and_replaced(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=providers())
        attempt = await mentor.start_exam(
            user_id, chapter_number=None, question_count=1, pass_percent=70
        )
        asked = await _ask(user_id, attempt.id)
        checked = await _check(user_id, asked, "Хорошо, но мало.")
        assert checked.answer == "Хорошо, но мало."
        assert (checked.exam.counted, checked.exam.answered, checked.exam.finished) == (
            False,
            0,
            False,
        )
        replacement = await _ask(user_id, attempt.id)  # вместо незасчитанного — другой
        assert (replacement.exam.turn, replacement.exam.answered) == (2, 0)


@pytest.mark.usefixtures("mentor_enabled")
async def test_changed_turn_or_open_question_is_refused_without_model(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, user_id = hotel
    fakes = providers()
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        attempt = await mentor.start_exam(
            user_id, chapter_number=None, question_count=3, pass_percent=70
        )
        with pytest.raises(AppError) as no_question:
            await _done(
                check_exam_answer(user_id, "Ответ", attempt_id=attempt.id, turn=1, providers=fakes)
            )
        await _ask(user_id, attempt.id)
        with pytest.raises(AppError) as open_question:  # вопрос открыт — сначала ответ
            await _done(next_exam_question(user_id, attempt.id, providers=fakes))
        with pytest.raises(AppError) as stale_turn:
            await _done(
                check_exam_answer(user_id, "Ответ", attempt_id=attempt.id, turn=7, providers=fakes)
            )
        with pytest.raises(AppError) as other_attempt:
            await _done(next_exam_question(user_id, uuid.uuid4(), providers=fakes))
    codes = {e.value.code for e in (no_question, open_question, stale_turn, other_attempt)}
    assert codes == {mentor.ERR_MENTOR_NO_EXAM_QUESTION}
    assert _calls(fakes) == 0


@pytest.mark.usefixtures("mentor_enabled")
async def test_question_of_vanished_article_is_dropped(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    fakes = providers()
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        attempt = await mentor.start_exam(
            user_id, chapter_number=2, question_count=3, pass_percent=70
        )
        asked = await mentor.set_exam_question(
            user_id, attempt.id, turn=0, article_number="3", question=QUESTION
        )
        # Новый кодекс без статьи 3 — она последняя, нумерация остаётся сплошной.
        trimmed = CODEX.split("#### Статья 3.")[0]
        await upload_codex(codex_files(trimmed), uploaded_by_user_id=None, providers=fakes)
        with pytest.raises(AppError) as error:
            await _done(
                check_exam_answer(
                    user_id, "Ответ", attempt_id=attempt.id, turn=asked.turn, providers=fakes
                )
            )
        assert error.value.code == mentor.ERR_MENTOR_NO_EXAM_QUESTION
        running = await mentor.current_exam(user_id)
        assert running is not None and running.current_question is None
        assert _calls(fakes) == 0
