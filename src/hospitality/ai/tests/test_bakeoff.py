"""Bake-off знает режим заявок — бесплатно, на Fake-провайдере (issue #427).

Платный прогон `ai/evals/bakeoff.py` в режиме «только консультации» (умолчание
`ENABLE_SERVICE_REQUESTS=false`) не должен валиться сквозным ассертом заявки
(#71): инструмента заявок у модели нет по построению, и провал был бы ложным —
деньги потрачены, вывод указывает на регрессию, которой нет. Режим учтён
находкой 17 ревью PR #344; эти тесты держат его. Парный тест в режиме `true`
показывает, что ассерт не снят, а включается режимом: тот же Fake без вызова
инструмента там валит прогон.
"""

from __future__ import annotations

import pytest

from hospitality.ai import orchestrator
from hospitality.ai.evals import bakeoff
from hospitality.ai.gateway.api import LlmProvider, MockLlmProvider
from hospitality.shared.config import get_settings

_CREATE_REQUEST_TOOL = "create_service_request"


def _offered_tools(provider: MockLlmProvider) -> set[str]:
    return {tool.name for request in provider.calls for tool in request.tools}


def _patch_candidates(monkeypatch: pytest.MonkeyPatch, provider: MockLlmProvider) -> str:
    """Кандидат прогона — активная модель рантайма, за ней Fake вместо API."""

    def build(model: str) -> LlmProvider:
        return provider

    monkeypatch.setattr(bakeoff, "build_provider", build)
    return get_settings().llm_model


async def test_consultation_mode_runs_without_the_request_assert(
    canonical_database: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    provider = MockLlmProvider(text="Оформить не могу — обратитесь на ресепшен.")
    model = _patch_candidates(monkeypatch, provider)

    await bakeoff.run([model], 1)

    out = capsys.readouterr().out
    assert "ENABLE_SERVICE_REQUESTS=False (только консультации)" in out
    assert "сквозной ассерт заявки НЕ гоняется" in out
    assert "гейт P-9 НЕ вооружён" not in out
    assert provider.calls, "прогон не дошёл до модели"
    assert _CREATE_REQUEST_TOOL not in _offered_tools(provider)


async def test_requests_mode_keeps_the_request_assert(
    canonical_database: None,
    service_requests_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider = MockLlmProvider(text="Хорошо, передам.")
    model = _patch_candidates(monkeypatch, provider)

    with pytest.raises(AssertionError, match="#71"):
        await bakeoff.run([model], 1)

    out = capsys.readouterr().out
    assert "ENABLE_SERVICE_REQUESTS=True (заявки включены)" in out
    assert "гейт P-9 НЕ вооружён" in out
    assert _CREATE_REQUEST_TOOL in _offered_tools(provider)


async def test_only_narrows_the_run_and_skips_the_request_assert(
    canonical_database: None,
    service_requests_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--only` без request: ни сценариев других видов, ни ассерта #71 — платный
    повтор нужного не платит за остальное."""
    provider = MockLlmProvider(text="Breakfast is from 07:00 to 10:30.")
    model = _patch_candidates(monkeypatch, provider)

    await bakeoff.run([model], 1, frozenset({"hotel_fact"}))

    out = capsys.readouterr().out
    assert "в --only нет request" in out
    assert "/request]" not in out
    hotel_facts = [s for s in bakeoff.HOTEL_FACT_SCENARIOS if s.kind == "hotel_fact"]
    assert len(provider.calls) == len(hotel_facts)


@pytest.mark.parametrize(
    ("language", "question", "caught"),
    [
        ("en", "Shall I ask housekeeping to clean room 305?", False),
        ("en", "Оформить заявку на доставку полотенец в номер 305?", True),
        ("zh", "要我为305房间提交清洁请求吗？", False),
        ("ru", "Оформить заявку на уборку номера 305?", False),
        ("ru", "Shall I ask housekeeping to clean room 305?", True),
        ("kk", "305 бөлмені тазалауға өтінім жіберейін бе?", False),
        ("kk", "Оформить заявку на уборку номера 305?", True),
        ("en", "", False),
    ],
)
def test_question_language_problem_by_script(language: str, question: str, caught: bool) -> None:
    """#342: вопрос-подтверждение на языке гостя — машинно, по письменности;
    казахский от русского — по буквам, которых в русском нет."""
    scenario = bakeoff.Scenario(language, "request", "-", "-")

    assert (bakeoff._question_language_problem(scenario, question) is not None) is caught


def _mixed_turn(question: str, guest_language: str) -> orchestrator.OrchestratorTurn:
    """Смешанный ход с предложенной заявкой — аргументы, как их пишет модель."""
    return orchestrator.OrchestratorTurn(
        kind=orchestrator.TurnKind.AWAITING_CONFIRMATION,
        reply_text=question,
        pending_action=orchestrator.PendingAction(
            tool_name=_CREATE_REQUEST_TOOL,
            arguments={"confirmation_question": question, "guest_language": guest_language},
        ),
    )


_ENGLISH_MIXED = "Breakfast is from 07:00 to 10:30. Shall I request towels for room 305?"


@pytest.mark.parametrize(
    ("question", "guest_language", "ok", "printed"),
    [
        (_ENGLISH_MIXED, "en", True, "Shall I request towels"),
        ("Завтрак с 07:00 до 10:30. Заказать полотенца в номер 305?", "en", False, "кириллицей"),
        (_ENGLISH_MIXED, "ru", False, "при госте en"),
    ],
)
def test_mixed_turn_prints_and_checks_guest_language(
    question: str, guest_language: str, ok: bool, printed: str
) -> None:
    """Ревью PR #1001: вопрос связан с `guest_language`, поэтому код языка
    печатается в исходе смешанного хода и сверяется с языком гостя — по печати
    видно, не он ли увёл вопрос в чужой язык."""
    scenario = next(
        s
        for s in bakeoff.HOTEL_FACT_SCENARIOS
        if s.kind == "hotel_fact_mixed" and s.language == "en"
    )

    passed, detail = bakeoff._check_hotel_fact_turn(scenario, _mixed_turn(question, guest_language))

    assert passed is ok
    assert printed in detail
    assert f"guest_language={guest_language!r}" in detail


def test_only_rejects_an_unknown_scenario_kind(capsys: pytest.CaptureFixture[str]) -> None:
    """Опечатка в `--only` не прогоняет ноль сценариев за деньги — отказ до вызова."""
    with pytest.raises(SystemExit):
        bakeoff._parse_args(["claude-sonnet-5", "--only", "requests,hotel_fact_mixed"])

    assert "неизвестные виды ['requests']" in capsys.readouterr().err
    assert bakeoff._parse_args(["--only", "request, hotel_fact_mixed"]).only
