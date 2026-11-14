"""Evals нескольких просьб в одном сообщении гостя на ru/kk/en (spec 0078 §7, issue #90).

Что мерим — правило `concierge_v9` «по вызову на каждую просьбу»:
- `two` — уборка и сломанный кондиционер: ровно два черновика, службы
  housekeeping и engineering — не слиты в один и не потерян второй;
- `three` — уборка, полотенца и такси: у eval-тенанта нет службы для такси,
  черновиков ровно два, а такси названо в реплике — не исчезло молча;
- `yes` — «да» на пакет `two`: созданы обе заявки (два вызова модели).

Ответ англичанину кириллицей — провал (как в bakeoff); ru и kk различает
человек по печати. Запуск ПЛАТНЫЙ (4 вызова модели на язык за повтор), только с
согласия основателя (CLAUDE.md); Postgres и ключ — как у bakeoff:

    python -m hospitality.ai.evals.multi_need [--repeat N] [модель ...]
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Literal

from hospitality.ai import orchestrator
from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.ai.gateway.api import LlmMessage, LlmProvider
from hospitality.ai.orchestrator import OrchestratorTurn, TurnKind
from hospitality.shared.alphabet import has_cyrillic
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

Kind = Literal["two", "three", "yes"]
ROOM = "305"

_TWO = {
    "ru": "Уберите, пожалуйста, номер и почините кондиционер — не работает.",
    "kk": "Нөмірді жинап беріңізші, кондиционерді де жөндеңіздерші — істемей тұр.",
    "en": "Please clean my room and fix the air conditioner — it isn't working.",
}
_THREE = {
    "ru": "Уберите номер, принесите полотенца и вызовите такси в аэропорт.",
    "kk": "Нөмірді жинаңыз, сүлгі әкеліңіз және әуежайға такси шақырыңызшы.",
    "en": "Please clean my room, bring fresh towels and call me a taxi to the airport.",
}
_YES = {"ru": "Да.", "kk": "Иә.", "en": "Yes, please."}
_TAXI = {"ru": "такси", "kk": "такси", "en": "taxi"}


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind


SCENARIOS = [
    Scenario(language, kind) for language in ("ru", "kk", "en") for kind in ("two", "three", "yes")
]


def _categories(turn: OrchestratorTurn) -> list[str]:
    if turn.pending_action is None:
        return []
    return [str(call.arguments.get("category_key")) for call in turn.pending_action.calls()]


def _check(scenario: Scenario, turn: OrchestratorTurn) -> tuple[bool, str]:
    reply = turn.reply_text
    categories = _categories(turn)
    if scenario.language == "en" and has_cyrillic(reply):
        return False, f"ответ англичанину кириллицей: {reply[:90]!r}"
    if scenario.kind == "yes":
        if turn.kind is not TurnKind.ACTION_DONE or len(turn.created_request_ids) != 2:
            created = len(turn.created_request_ids)
            return False, f"на «да» создано {created} (kind={turn.kind.value}): {reply[:90]!r}"
        return True, reply
    if turn.kind is not TurnKind.AWAITING_CONFIRMATION or len(categories) != 2:
        return (
            False,
            f"черновиков {len(categories)} {categories} ({turn.kind.value}): {reply[:90]!r}",
        )
    if scenario.kind == "two" and sorted(categories) != ["engineering", "housekeeping"]:
        return False, f"службы {categories}: {reply[:90]!r}"
    if scenario.kind == "three" and _TAXI[scenario.language] not in reply.lower():
        return False, f"такси исчезло из ответа: {reply[:90]!r}"
    return True, f"{categories} {reply}"


async def _run_one(provider: LlmProvider, scenario: Scenario) -> tuple[bool, str]:
    message = (_THREE if scenario.kind == "three" else _TWO)[scenario.language]
    with tenant_context(await _eval_tenant_id()):
        turn = await orchestrator.handle_message(
            message=message, provider=provider, verified_room_number=ROOM
        )
        if scenario.kind != "yes":
            return _check(scenario, turn)
        if turn.pending_action is None or not turn.pending_action.more:
            return False, f"пакета нет ({turn.kind.value}): {turn.reply_text[:90]!r}"
        done = await orchestrator.handle_message(
            message=_YES[scenario.language],
            history=[
                LlmMessage(role="user", content=message),
                LlmMessage(role="assistant", content=turn.reply_text),
            ],
            pending_action=turn.pending_action,
            provider=provider,
            verified_room_number=ROOM,
        )
    return _check(scenario, done)


async def run(models: list[str], repeat: int) -> None:
    # Правило живёт в разделе `# Service requests`, который режим консультаций снимает.
    os.environ["ENABLE_SERVICE_REQUESTS"] = "true"
    get_settings.cache_clear()
    providers = _build_candidates(models)
    if not providers:
        print("Ни один кандидат не собран — причины выше.")
        return
    await _ensure_eval_tenant()
    failures = 0
    for model, provider in providers.items():
        print(f"\n===== {model}, повторов: {repeat} =====")
        for scenario in SCENARIOS:
            for _ in range(repeat):
                try:
                    ok, detail = await _run_one(provider, scenario)
                except AppError as error:
                    ok, detail = False, f"ERROR {error.code}: {error.message}"
                failures += not ok
                print(f"  {'OK ' if ok else '!! '}[{scenario.language}/{scenario.kind}] {detail}")
    print(f"\nПровалов: {failures}.")
    if failures:
        sys.exit(1)


def _parse(argv: list[str]) -> tuple[list[str], int]:
    repeat = 1
    if "--repeat" in argv:
        index = argv.index("--repeat")
        repeat = int(argv[index + 1])
        argv = argv[:index] + argv[index + 2 :]
    return argv or [get_settings().llm_model], repeat


if __name__ == "__main__":
    asyncio.run(run(*_parse(sys.argv[1:])))
