"""Evals промпта «Предложить идею» (spec 0071 §11 п. 3, FOUNDATION §7.7).

12 идей сотрудников: 4 — то, что в Necturn уже есть, 4 — против Non-Goals NG-2…NG-6
(свой PMS, хранение карт, Excel как база, неофициальный WhatsApp), 4 — новые; по 2–3
хода, 33 вызова. Ход — тот же `build_request`, что у кабинета; ответы автора между
ходами заданы сценарием. Машинно проверяется то, чего не проверит юнит-тест, где ответ
модели подставляет сам тест:
- ни в одном ответе нет окончательного отказа («нельзя», «невозможно», «не получится»
  без «на первый взгляд»; «мүмкін емес»; «impossible»);
- нет обещаний и сроков («сделаем», «будет готово», «через N дней», «we will»);
- ответ на языке последнего сообщения автора (ru, kk, en);
- к последнему ходу карточка предложена; у «уже есть» — пометка `exists` или `similar`,
  у Non-Goals — `constraint` или `unknown` (печатается, решает человек).

Запуск ПЛАТНЫЙ (33 вызова модели идей, около $0,5 на Sonnet 5), только с согласия
основателя (CLAUDE.md); Postgres и ключ — как у bakeoff:

    python -m hospitality.ai.evals.staff_ideas [модель ...]
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.ideas import IDEA_DRAFT_LABEL, build_request
from hospitality.ai.tools import propose_idea_card
from hospitality.modules.mentor import api as mentor
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

SEND_AS_IS: Final = {
    "ru": "Да, так и отправьте.",
    "kk": "Иә, осылай жіберіңіз.",
    "en": "Yes, send it.",
}


@dataclass(frozen=True)
class Case:
    group: str  # exists | non_goal | new
    language: str
    turns: tuple[str, ...]


def _case(group: str, language: str, idea: str, *details: str) -> Case:
    return Case(group, language, (idea, *details, SEND_AS_IS[language]))


CASES: Final = (
    _case("exists", "ru", "Хочу видеть на телефоне, какие номера уже убраны после выезда."),
    _case(
        "exists",
        "ru",
        "Пусть гость сам пишет боту про полотенца, а не звонит на ресепшен.",
        "Сейчас звонят раз двадцать за смену, администратор записывает на бумажку.",
    ),
    _case("exists", "kk", "Жаңа қызметкерге отельдің ережелерін үйрететін көмекші керек."),
    _case(
        "exists",
        "ru",
        "Нужно, чтобы заявку, которую никто не взял, видел старший.",
        "Бывает, заявка висит час, а горничная на другом этаже.",
    ),
    _case(
        "non_goal",
        "ru",
        "Давайте сделаем свою систему бронирования вместо Opera, она медленная.",
        "Чтобы найти бронь, нужно пять экранов, гость ждёт у стойки.",
    ),
    _case(
        "non_goal",
        "ru",
        "Пусть бот сохраняет карту гостя и сам списывает за мини-бар.",
        "Сейчас на выезде пересчитываем мини-бар, гость торопится, очередь.",
    ),
    _case(
        "non_goal",
        "ru",
        "Хочу, чтобы все заявки хранились в Google Таблице, а не в системе.",
        "Менеджеру удобнее фильтровать в таблице, он к ней привык.",
    ),
    _case(
        "non_goal",
        "en",
        "Connect the bot to my personal WhatsApp through some app, guests write there anyway.",
        "Guests send photos of broken things, I forward them to engineering by hand.",
    ),
    _case(
        "new",
        "ru",
        "Нужен общий список забытых вещей с фото, сейчас всё в тетради.",
        "Гость звонит через неделю, ищем полчаса, тетрадь у другой смены.",
    ),
    _case(
        "new",
        "kk",
        "Түнгі ауысымға таңертеңгі ауысымнан қысқаша есеп автоматты түрде келсе екен.",
        "Қазір бәрін ауызша айтамыз, бір нәрсе ұмытылады.",
    ),
    _case("new", "ru", "Пусть бот напоминает гостю про поздний выезд за час."),
    _case(
        "new",
        "ru",
        "Хочу видеть, сколько раз за неделю ломался один и тот же номер.",
        "Инженер чинит кондиционер в 512 уже третий раз, никто не замечает.",
    ),
)

_FINAL_NO: Final = re.compile(
    r"\b(?:нельзя|невозможно|не получится|не сможем|мүмкін емес|болмайды|impossible|"
    r"cannot be done|can't be done|not possible)\b",
    re.IGNORECASE,
)
_SOFTENER: Final = re.compile(r"на первый взгляд|бірінші көзқараста|at first glance", re.IGNORECASE)
_PROMISE: Final = re.compile(
    r"\b(?:сделаем|реализуем|добавим|будет готово|через \d+ (?:дн|недел|месяц)|"
    r"в течение (?:\d+ )?(?:дн|недел|месяц)|жасаймыз|we will|we'll)",
    re.IGNORECASE,
)
_CYRILLIC: Final = re.compile(r"[а-яё]", re.IGNORECASE)
_KAZAKH: Final = re.compile(r"[әғқңөұүһі]", re.IGNORECASE)
_EXPECTED_MARKS: Final = {
    "exists": {mentor.IdeaMark.EXISTS, mentor.IdeaMark.SIMILAR},
    "non_goal": {mentor.IdeaMark.CONSTRAINT, mentor.IdeaMark.UNKNOWN},
}


def text_problems(language: str, text: str) -> list[str]:
    """Провалы одного текста автору (ответа или пересказа); пусто — прошёл."""
    problems: list[str] = []
    if _FINAL_NO.search(text) and not _SOFTENER.search(text):
        problems.append("окончательный отказ")
    if _PROMISE.search(text):
        problems.append("обещание или срок")
    if language == "en" and _CYRILLIC.search(text):
        problems.append("не по-английски")
    elif language == "kk" and not _KAZAKH.search(text):
        problems.append("не по-казахски")
    elif language == "ru" and _KAZAKH.search(text):
        problems.append("по-казахски вместо русского")
    return problems


def _message(role: mentor.MessageRole, text: str) -> mentor.MentorMessageRead:
    return mentor.MentorMessageRead(
        id=uuid.uuid4(),
        role=role,
        text=text,
        cited_articles=[],
        in_summary=False,
        created_at=utc_now(),
    )


async def _run_case(case: Case, provider: gateway.LlmProvider) -> tuple[list[str], Decimal]:
    history: list[mentor.MentorMessageRead] = []
    draft: str | None = None
    mark: mentor.IdeaMark | None = None
    problems: list[str] = []
    cost = Decimal(0)
    for text in case.turns:
        request = build_request(mentor.ConversationMode.IDEA, history, text, draft=draft)
        response = await gateway.complete(request, provider=provider)
        cost += response.cost_usd
        call = next((c for c in response.tool_calls if c.name == propose_idea_card.NAME), None)
        card = propose_idea_card.parse(call.arguments) if call is not None else None
        reply = response.text.strip()
        print(f"    автор: {text}\n    ИИ:    {reply or '—'}")
        problems += text_problems(case.language, reply) if reply else []
        if card is not None:
            draft, mark = card.summary, card.mark
            print(f"    карточка [{card.mark.value}] {card.mark_note or ''}\n      {card.summary}")
            problems += [f"пересказ: {p}" for p in text_problems(case.language, card.summary)]
        elif not reply:
            problems.append("пустой ответ без карточки")
        answer = f"{reply}\n\n{IDEA_DRAFT_LABEL}" if card and reply else reply or IDEA_DRAFT_LABEL
        history += [
            _message(mentor.MessageRole.USER, text),
            _message(mentor.MessageRole.ASSISTANT, answer),
        ]
    if mark is None:
        problems.append("карточка не предложена")
    elif case.group in _EXPECTED_MARKS and mark not in _EXPECTED_MARKS[case.group]:
        problems.append(f"пометка {mark.value} — проверить глазами")
    return problems, cost


async def run(models: list[str]) -> None:
    providers = _build_candidates(models)
    if not providers:
        print("Ни один кандидат не собран — причины выше.")
        return
    await _ensure_eval_tenant()
    tenant_id = await _eval_tenant_id()
    failures = 0
    for model, provider in providers.items():
        # Назначение `ideas` включено моделью кандидата: пустая модель — ERR-AI-006.
        os.environ["LLM_MODEL_IDEAS"] = model
        get_settings.cache_clear()
        total = Decimal(0)
        print(
            f"\n===== {model}: {len(CASES)} идей, {sum(len(c.turns) for c in CASES)} вызовов ====="
        )
        for case in CASES:
            print(f"\n  [{case.group}/{case.language}]")
            try:
                with tenant_context(tenant_id):
                    problems, cost = await _run_case(case, provider)
            except AppError as error:
                failures += 1
                print(f"  !! ERROR {error.code}: {error.message}")
                continue
            total += cost
            failures += bool(problems)
            print("  OK" if not problems else "  !! " + "; ".join(problems))
        print(f"\n  стоимость прогона: ${total:.4f}")
    print(f"\nПровалов: {failures}. Ответы и пометки — прочесть глазами (spec 0071 §6).")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run(sys.argv[1:] or [get_settings().llm_model_ideas or "claude-sonnet-5"]))
