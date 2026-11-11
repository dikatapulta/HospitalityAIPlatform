"""Evals справочника города на 6 языках пилота (spec 0085 §8, FOUNDATION §7.7, ADR-010).

Что мерим — что слышит гость, когда у отеля включён справочник Алматы:
- `covered` — «как добраться до Медеу?»: ответ из справочника и ссылка карты
  ровно та, что построил код (машинно — ссылка в ответе дословно);
- `unknown` — «где лучшие суши?»: справочник молчит — бот не выдумывает,
  ведёт на ресепшен и зовёт `report_unanswered_question` (машинно — сигнал);
- `price` — «сколько стоит билет на Кок-Тобе?»: место в справочнике есть, цены
  нет — цифры с валютой в ответе нет (машинно), ресепшен — по печати.

Отделяет ли бот город от отеля («это общая информация, не услуга отеля»),
оценивает человек по печати. Режим — «только консультации», как на гостевой
копии. Конфиг eval-тенанта bakeoff получает `city_guide_enabled` — следующий
прогон bakeoff его перезапишет. Запуск ПЛАТНЫЙ (18 ходов модели гостя на
повтор; цена — spec 0085 §8), только с согласия основателя (CLAUDE.md);
Postgres и ключ — как у bakeoff:

    python -m hospitality.ai.evals.city_guide [--repeat N] [модель ...]
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from typing import Literal

from hospitality.ai import orchestrator
from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.modules.concierge import api as concierge_api
from hospitality.platform.config import load_tenant_config, store_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

Kind = Literal["covered", "unknown", "price"]
_KINDS: tuple[Kind, ...] = ("covered", "unknown", "price")

# Места, о которых спрашивают сценарии, — по началу темы записи справочника.
_COVERED_TOPIC = "Medeu"
_PRICE_TOPIC = "Kok-Tobe"


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind
    message: str


_MESSAGES: dict[str, tuple[str, str, str]] = {
    "en": (
        "How do I get to Medeu?",
        "Which restaurant in Almaty has the best sushi?",
        "How much is a ticket to Kok-Tobe?",
    ),
    "ru": (
        "Как добраться до Медеу?",
        "В каком ресторане Алматы лучшие суши?",
        "Сколько стоит билет на Кок-Тобе?",
    ),
    "kk": (
        "Медеуге қалай жетуге болады?",
        "Алматыда қай мейрамханада ең дәмді суши бар?",
        "Көктөбеге билет қанша тұрады?",
    ),
    "zh": (
        "怎么去 Medeu？",
        "阿拉木图哪家餐厅的寿司最好？",
        "去 Kok-Tobe 的票多少钱？",
    ),
    "tr": (
        "Medeu'ya nasıl gidebilirim?",
        "Almatı'da en iyi suşi hangi restoranda?",
        "Kok-Tobe bileti ne kadar?",
    ),
    "hi": (
        "मैं Medeu कैसे पहुँचूँ?",
        "अल्माटी में सबसे अच्छी सुशी किस रेस्टोरेंट में मिलती है?",
        "Kok-Tobe का टिकट कितने का है?",
    ),
}

SCENARIOS: list[Scenario] = [
    Scenario(language, kind, message)
    for language, messages in _MESSAGES.items()
    for kind, message in zip(_KINDS, messages, strict=True)
]

# Цена — число рядом с валютой, в любом порядке.
_PRICE = re.compile(r"\d[\d\s.,]*\s*(₸|tenge|kzt|тенге|теңге|\$|€)|(₸|kzt|\$|€)\s*\d", re.I)


def _map_url(topic_start: str) -> str:
    guide = concierge_api.city_guide_for("Almaty", "KZ")
    assert guide is not None
    entry = next(entry for entry in guide.entries if entry.topic.startswith(topic_start))
    url = guide.map_url(entry)
    assert url is not None
    return url


def _check(scenario: Scenario, turn: orchestrator.OrchestratorTurn) -> tuple[bool, str]:
    reply = turn.reply_text
    if turn.kind is orchestrator.TurnKind.NEEDS_HUMAN or turn.pending_action is not None:
        return False, f"вместо ответа — {turn.kind.value}: {reply[:90]!r}"
    if scenario.kind == "covered" and _map_url(_COVERED_TOPIC) not in reply:
        return False, f"нет ссылки карты из справочника: {reply[:120]!r}"
    if scenario.kind == "unknown" and turn.unanswered_question is None:
        return False, f"сигнал report_unanswered_question не вызван: {reply[:90]!r}"
    if _PRICE.search(reply):
        return False, f"названа цена: {reply[:90]!r}"
    return True, reply


async def _ensure_city_guide_tenant() -> None:
    """Eval-тенант bakeoff с включённым справочником города (профиль — Алматы)."""
    await _ensure_eval_tenant()
    tenant_id = await _eval_tenant_id()
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
        await store_tenant_config(
            session, tenant_id, config.model_copy(update={"city_guide_enabled": True})
        )


async def run(models: list[str], repeat: int) -> None:
    providers = _build_candidates(models)
    if not providers:
        print("Ни один кандидат не собран — причины выше.")
        return
    os.environ["ENABLE_SERVICE_REQUESTS"] = "false"
    get_settings.cache_clear()
    await _ensure_city_guide_tenant()
    tenant_id = await _eval_tenant_id()
    failures = 0
    for model, provider in providers.items():
        print(f"\n===== {model}, повторов: {repeat} =====")
        for scenario in SCENARIOS:
            for _ in range(repeat):
                try:
                    with tenant_context(tenant_id):
                        turn = await orchestrator.handle_message(
                            message=scenario.message, provider=provider
                        )
                    ok, detail = _check(scenario, turn)
                except AppError as error:
                    ok, detail = False, f"ERROR {error.code}: {error.message}"
                failures += not ok
                print(f"  {'OK ' if ok else '!! '}[{scenario.language}/{scenario.kind}] {detail}")
    print(f"\nПровалов: {failures}. Ответы — прочесть глазами (spec 0085 §8).")
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
