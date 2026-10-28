"""Evals инструмента `escalate_to_staff` на языках пилота ru/kk/en (spec 0040 §5).

Что мерим — обе стороны одной границы (комментарий к #101 из spec 0036):
- `ask` и `accept` — гость просит человека или говорит «да» на предложение:
  ход обязан стать эскалацией `guest_requested_staff`, а не словами;
- `offer` и `fact` — вопрос, на который бот не знает ответа, и вопрос из
  справочника: эскалации быть НЕ должно, иначе частые вопросы первого дня
  завалят кабинет карточками;
- `consult` — режим консультаций (флаг заявок выключен): инструмента нет,
  обещания сотрудника быть не должно; текст оценивает человек по печати.

Ответ англичанину кириллицей — провал (язык аргумента, #342). Запуск ПЛАТНЫЙ,
только с согласия основателя (CLAUDE.md); Postgres и ключ — как у bakeoff:

    python -m hospitality.ai.evals.staff_escalation [--repeat N] [модель ...]
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from typing import Literal

from hospitality.ai import orchestrator
from hospitality.ai.escalation import EscalationReason
from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.ai.gateway.api import LlmMessage, LlmProvider
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

Kind = Literal["ask", "accept", "offer", "fact", "consult"]

# Предложение модели прошлого хода — вход сценария `accept`. Текстом, как его
# хранит история диалога: свободная реплика REPLY, pending_action у неё нет.
_OFFERS = {
    "ru": ("Сколько стоит поздний выезд?", "Цену не знаю. Позвать сотрудника, чтобы уточнил?"),
    "kk": ("Кеш шығу қанша тұрады?", "Бағасын білмеймін. Қызметкерді шақырайын ба?"),
    "en": ("How much is a late checkout?", "I don't know the price. Shall I ask a staff member?"),
}


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind
    message: str


SCENARIOS: list[Scenario] = [
    Scenario("ru", "ask", "Позовите, пожалуйста, менеджера."),
    Scenario("kk", "ask", "Менеджерді шақырыңызшы."),
    Scenario("en", "ask", "Can I talk to a real person, please?"),
    Scenario("ru", "accept", "Да, позовите."),
    Scenario("kk", "accept", "Иә, шақырыңыз."),
    Scenario("en", "accept", "Yes, please."),
    Scenario("ru", "offer", "Сколько стоит поздний выезд?"),
    Scenario("kk", "offer", "Кеш шығу қанша тұрады?"),
    Scenario("en", "offer", "How much is a late checkout?"),
    Scenario("ru", "fact", "Во сколько завтрак?"),
    Scenario("kk", "fact", "Таңғы ас нешеде?"),
    Scenario("en", "fact", "What time is breakfast?"),
    Scenario("ru", "consult", "Позовите, пожалуйста, менеджера."),
    Scenario("kk", "consult", "Менеджерді шақырыңызшы."),
    Scenario("en", "consult", "Can I talk to a real person, please?"),
]


def _set_requests_mode(enabled: bool) -> None:
    """Флаг заявок на один сценарий — инструмент живёт ровно под ним (spec 0040 §2)."""
    os.environ["ENABLE_SERVICE_REQUESTS"] = "true" if enabled else "false"
    get_settings.cache_clear()


def _check(scenario: Scenario, turn: orchestrator.OrchestratorTurn) -> tuple[bool, str]:
    escalated = (
        turn.escalation is not None
        and turn.escalation.reason is EscalationReason.GUEST_REQUESTED_STAFF
    )
    if scenario.kind in ("ask", "accept") and not escalated:
        return False, f"эскалации НЕТ (kind={turn.kind.value}): {turn.reply_text[:90]!r}"
    if scenario.kind in ("offer", "fact", "consult") and escalated:
        return False, f"лишняя эскалация: {turn.reply_text[:90]!r}"
    # Машинно ловится только «латиница vs кириллица» (как в bakeoff): ru и kk
    # различает человек по печати.
    if scenario.language == "en" and any("а" <= char.lower() <= "я" for char in turn.reply_text):
        return False, f"ответ англичанину кириллицей: {turn.reply_text[:90]!r}"
    return True, turn.reply_text


async def _run_one(provider: LlmProvider, scenario: Scenario) -> tuple[bool, str]:
    _set_requests_mode(scenario.kind != "consult")
    history: list[LlmMessage] = []
    if scenario.kind == "accept":
        question, offer = _OFFERS[scenario.language]
        history = [
            LlmMessage(role="user", content=question),
            LlmMessage(role="assistant", content=offer),
        ]
    with tenant_context(await _eval_tenant_id()):
        turn = await orchestrator.handle_message(
            message=scenario.message, history=history, provider=provider
        )
    return _check(scenario, turn)


async def run(models: list[str], repeat: int) -> None:
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
