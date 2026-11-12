"""Обвязка платных evals `evals/memo_format` — бесплатно, без модели (spec 0077 §11 п. 6).

Набор — 10 записок на трёх языках, 30 вызовов (число плана прогона); проверка ловит
добавленное число, чужой язык, разметку, шапку и заполненную строку шаблона.
"""

from __future__ import annotations

from hospitality.ai.evals import memo_format as evals

LIFT = next(case for case in evals.CASES if case.key == "lift")
LEAK = next(case for case in evals.CASES if case.key == "leak")


def test_set_is_thirty_calls_on_known_templates() -> None:
    assert len(evals.CASES) == 10
    assert all(set(case.texts) == {"ru", "kk", "en"} for case in evals.CASES)
    assert all(case.template in (None, *evals.TEMPLATES) for case in evals.CASES)


def test_clean_answer_passes() -> None:
    answer = (
        "Где: лифт 2, между 4 и 5 этажом.\nЧто неисправно и с какого времени: с утра "
        "останавливается.\nЧем мешает гостям или работе: …\nПрошу: …"
    )
    assert evals.answer_problems(LIFT, "ru", answer) == []


def test_each_failure_is_named() -> None:
    assert evals.answer_problems(LEAK, "ru", "Где: номер 412, 3 раза за 24 часа.") == [
        "добавлено число 24",
        "добавлено число 3",
    ]
    assert evals.answer_problems(LEAK, "en", "Где: номер 412.") == ["не по-английски"]
    assert evals.answer_problems(LEAK, "kk", "Где: номер 412.") == ["не по-казахски"]
    assert evals.answer_problems(LEAK, "ru", "**Где:** номер 412.") == ["разметка Markdown"]
    assert evals.answer_problems(LEAK, "ru", "Директору. Номер 412.") == ["шапка или подпись"]
    assert evals.answer_problems(LIFT, "ru", "Лифт 2 встаёт между 4 и 5 этажом.") == [
        "строка шаблона без ответа заполнена или пропала"
    ]
