"""Замер кэша промпта гостя на живой модели (issue #138).

Два хода одного диалога eval-тенанта (справочник отеля, комната 305) через
оркестратор и шлюз. Первый ход пишет стабильную часть промпта в кэш, второй
обязан её прочитать: `cache_read_input_tokens > 0` у первого вызова модели во
втором ходе. Ноль — точка кэша стоит не там или префикс меняется от хода к ходу
(молчаливый промах: ход дорожает на 25 %, а ошибки нет).

Печатает токены и стоимость каждого вызова. Запуск ПЛАТНЫЙ (2–3 вызова Sonnet 5,
около $0,03), только с согласия основателя (CLAUDE.md); Postgres и ключ — как у
bakeoff:

    python -m hospitality.ai.evals.prompt_cache [модель]
"""

from __future__ import annotations

import asyncio
import os
import sys

from hospitality.ai import orchestrator
from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.ai.gateway.api import LlmMessage, LlmProvider, LlmProviderResult, LlmRequest
from hospitality.shared.config import get_settings
from hospitality.shared.tenancy import tenant_context

ROOM = "305"
_FIRST = "Здравствуйте! Во сколько завтрак?"
_SECOND = "А до скольки работает бассейн?"


class _Recorder:
    """Обёртка провайдера: копит ответы, чтобы напечатать токены кэша каждого вызова."""

    def __init__(self, inner: LlmProvider) -> None:
        self._inner = inner
        self.results: list[LlmProviderResult] = []

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def model(self) -> str:
        return self._inner.model

    async def complete(self, request: LlmRequest) -> LlmProviderResult:
        result = await self._inner.complete(request)
        self.results.append(result)
        return result


def _print_calls(label: str, results: list[LlmProviderResult]) -> None:
    for index, result in enumerate(results, start=1):
        print(
            f"  {label}, вызов {index}: вход {result.input_tokens} "
            f"(запись в кэш {result.cache_creation_input_tokens}, "
            f"чтение из кэша {result.cache_read_input_tokens}), выход {result.output_tokens}"
        )


async def measure(provider: LlmProvider) -> tuple[list[LlmProviderResult], list[LlmProviderResult]]:
    """Два хода одного диалога eval-тенанта: ответы модели первого и второго хода."""
    recorder = _Recorder(provider)
    with tenant_context(await _eval_tenant_id()):
        first = await orchestrator.handle_message(
            message=_FIRST, provider=recorder, verified_room_number=ROOM
        )
        first_calls = list(recorder.results)
        recorder.results.clear()
        await orchestrator.handle_message(
            message=_SECOND,
            history=[
                LlmMessage(role="user", content=_FIRST),
                LlmMessage(role="assistant", content=first.reply_text),
            ],
            provider=recorder,
            verified_room_number=ROOM,
        )
    return first_calls, recorder.results


async def run(model: str) -> None:
    # Заявки включены, как у живого отеля после этапа 3: состав инструментов —
    # часть кэшируемого префикса, и мерить надо тот, что увидит гость.
    os.environ["ENABLE_SERVICE_REQUESTS"] = "true"
    get_settings.cache_clear()
    providers = _build_candidates([model])
    if not providers:
        sys.exit(1)
    await _ensure_eval_tenant()
    first_calls, second_calls = await measure(providers[model])
    print(f"===== {model} =====")
    _print_calls("ход 1", first_calls)
    _print_calls("ход 2", second_calls)
    read = second_calls[0].cache_read_input_tokens if second_calls else 0
    if read <= 0:
        print("!! второй ход не прочитал кэш — точка кэша не работает")
        sys.exit(1)
    print(f"OK второй ход прочитал из кэша {read} токенов")


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1] if len(sys.argv) > 1 else get_settings().llm_model))
