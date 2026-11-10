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
