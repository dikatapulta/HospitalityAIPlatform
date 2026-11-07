"""Evals напоминания гостя о заявке на ru/kk/en (spec 0070 §8, issue #834).

Что мерим — граница одного правила `concierge_v8` («The guest's active requests»)
с соседями. У диалога одна открытая заявка — полотенца, статус «ждёт»:
- `remind` — «где мои полотенца?»: напоминание ровно на эту заявку (счёт на
  заявке +1), реплика на языке гостя без срока и без «сообщил менеджеру»;
- `thanks` и `new` — «спасибо, принесли» и новая просьба: напоминания нет,
  у новой просьбы — черновик заявки в гейте P-9;
- `mixed` — «где полотенца? и принесите воды»: и напоминание, и черновик заявки
  (сопутствующий вызов у действия не выигрывает);
- `cancel` — «отмените»: черновик отмены, напоминания нет;
- `absent` — о заявке, которой нет в списке: напоминания нет, вместо него —
  честное «не вижу такой заявки» и предложение сотрудника (читает человек);
- `manager` — «позовите менеджера, я второй раз прошу!»: `escalate_to_staff`.

Машинно ловятся: эффект на заявке, вид хода, кириллица у англичанина, срок
числом («через 10 минут») и слово «менеджер» в реплике напоминания. Остальное —
по печати. Запуск ПЛАТНЫЙ (21 ход модели гостя на повтор), только с согласия
основателя (CLAUDE.md); Postgres и ключ — как у bakeoff:

    python -m hospitality.ai.evals.request_reminder [--repeat N] [модель ...]
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from typing import Literal

from hospitality.ai import orchestrator
from hospitality.ai.escalation import EscalationReason
from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.ai.gateway.api import LlmProvider
from hospitality.ai.tools.base import ActiveRequest
from hospitality.modules.requests import api as requests_api
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

Kind = Literal["remind", "thanks", "new", "mixed", "cancel", "absent", "manager"]

# Открытая заявка диалога — на языке гостя, как её пишет модель при создании.
_TOWELS = {"ru": "Принести полотенца", "kk": "Сүлгі әкелу", "en": "Bring towels"}


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind
    message: str


SCENARIOS: list[Scenario] = [
    Scenario("ru", "remind", "Где мои полотенца? Я просил час назад."),
    Scenario("kk", "remind", "Сүлгілерім қайда? Бір сағат бұрын сұрағанмын."),
    Scenario("en", "remind", "Where are my towels? I asked an hour ago."),
    Scenario("ru", "thanks", "Спасибо, полотенца принесли!"),
    Scenario("kk", "thanks", "Рахмет, сүлгілерді әкелді!"),
    Scenario("en", "thanks", "Thanks, the towels just arrived!"),
    Scenario("ru", "new", "Принесите, пожалуйста, бутылку воды."),
    Scenario("kk", "new", "Бір бөтелке су әкеліңізші."),
    Scenario("en", "new", "Could you bring me a bottle of water, please?"),
    Scenario("ru", "mixed", "Где мои полотенца? И принесите воды, пожалуйста."),
    Scenario("kk", "mixed", "Сүлгілерім қайда? Және су әкеліңізші."),
    Scenario("en", "mixed", "Where are my towels? And please bring some water."),
    Scenario("ru", "cancel", "Отмените заявку на полотенца, уже не нужно."),
    Scenario("kk", "cancel", "Сүлгіге өтінімді болдырмаңыз, енді керек емес."),
    Scenario("en", "cancel", "Please cancel the towels request, I don't need them anymore."),
    Scenario("ru", "absent", "Где мой утюг? Я заказывал его утром."),
    Scenario("kk", "absent", "Үтігім қайда? Таңертең сұрағанмын."),
    Scenario("en", "absent", "Where is my iron? I ordered it this morning."),
    Scenario("ru", "manager", "Позовите менеджера, я уже второй раз прошу полотенца!"),
    Scenario("kk", "manager", "Менеджерді шақырыңыз, сүлгіні екінші рет сұрап отырмын!"),
    Scenario("en", "manager", "Get me a manager, this is the second time I ask for towels!"),
]

# Срок числом и упоминание менеджера в реплике напоминания (spec 0070 §4):
# грубая машинная сетка, тонкое — по печати.
_TIME_PROMISE = re.compile(r"\d+\s*(мин|сағ|час|min|hour)", re.IGNORECASE)
_MANAGER = re.compile(r"менеджер|manager", re.IGNORECASE)


def _enable_requests() -> None:
    """Флаг заявок: `new` и `mixed` без него не создадут черновик."""
    os.environ["ENABLE_SERVICE_REQUESTS"] = "true"
    get_settings.cache_clear()


async def _open_towels_request(language: str) -> requests_api.ServiceRequestRead:
    categories = await requests_api.list_categories()
    category_id = next(c.id for c in categories if c.key == "housekeeping")
    return await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=category_id,
            origin=requests_api.ServiceRequestOrigin.GUEST_CHAT,
            summary=_TOWELS[language],
            room_number="305",
        )
    )


def _check(
    scenario: Scenario, turn: orchestrator.OrchestratorTurn, reminded: int
) -> tuple[bool, str]:
    pending = turn.pending_action.tool_name if turn.pending_action is not None else None
    reply = turn.reply_text
    expected_reminders = 1 if scenario.kind in ("remind", "mixed") else 0
    if scenario.kind != "manager" and reminded != expected_reminders:
        return False, f"напоминаний {reminded}, ждали {expected_reminders}: {reply[:90]!r}"
    if scenario.kind in ("new", "mixed") and pending != "create_service_request":
        return False, f"черновика заявки НЕТ (kind={turn.kind.value}): {reply[:90]!r}"
    if scenario.kind == "cancel" and pending != "cancel_service_request":
        return False, f"черновика отмены НЕТ (kind={turn.kind.value}): {reply[:90]!r}"
    escalated = (
        turn.escalation is not None
        and turn.escalation.reason is EscalationReason.GUEST_REQUESTED_STAFF
    )
    if scenario.kind == "manager" and not escalated:
        return False, f"эскалации НЕТ (kind={turn.kind.value}): {reply[:90]!r}"
    if scenario.kind != "manager" and escalated:
        return False, f"лишняя эскалация: {reply[:90]!r}"
    if scenario.kind in ("remind", "mixed"):
        if _TIME_PROMISE.search(reply):
            return False, f"обещан срок: {reply[:90]!r}"
        if _MANAGER.search(reply):
            return False, f"«менеджер» в реплике: {reply[:90]!r}"
    if scenario.language == "en" and any("а" <= char.lower() <= "я" for char in reply):
        return False, f"ответ англичанину кириллицей: {reply[:90]!r}"
    return True, f"[напоминаний {reminded}, {turn.kind.value}] {reply}"


async def _run_one(provider: LlmProvider, scenario: Scenario) -> tuple[bool, str]:
    with tenant_context(await _eval_tenant_id()):
        request = await _open_towels_request(scenario.language)
        snapshot = ActiveRequest(
            id=request.id,
            status=request.status,
            summary=request.summary,
            daily_number=request.daily_number,
            room_number=request.room_number,
        )
        turn = await orchestrator.handle_message(
            message=scenario.message, active_requests=[snapshot], provider=provider
        )
        reminded = (await requests_api.get_request(request.id)).guest_reminder_count
        # Не копить открытые заявки в eval-тенанте: следующий прогон их не видит,
        # а кабинет — видит.
        await requests_api.change_request_status(
            request.id,
            requests_api.RequestStatus.CANCELLED,
            initiator=requests_api.RequestInitiator.GUEST,
        )
    return _check(scenario, turn, reminded)


async def run(models: list[str], repeat: int) -> None:
    providers = _build_candidates(models)
    if not providers:
        print("Ни один кандидат не собран — причины выше.")
        return
    _enable_requests()
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
