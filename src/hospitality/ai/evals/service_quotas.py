"""Evals квоты бесплатного: ставит ли модель ключ квоты (spec 0056 §7, FOUNDATION §7.7).

Что мерим — только аргументы модели, остальное решает код и проверено юнит-тестами
(`ai/tests/test_service_quota.py`): «принесите воды» → `quota_key=water`, 1;
«две бутылки воды» → 2; «полотенца», «кофе в пакетиках» → без ключа. Ложный
ключ — провал (гость получит чужую норму или платное предложение), пропуск — в
отчёт: заявка уходит бесплатно, как до квот, цена ошибки — бутылка. Языки —
набор пилота en/ru/kk/zh/tr/hi.

Ход идёт с проживанием и нормой с запасом (20 × 20 гостей): метка модели
проходит `prepare` и видна в `pending_action` такой, какой её получит заявка —
кривое число код превратил бы в 1, и это тоже провал. Запуск ПЛАТНЫЙ (24 хода
модели гостя), только с согласия основателя (CLAUDE.md); Postgres и ключ — как
у bakeoff:

    python -m hospitality.ai.evals.service_quotas [--repeat N] [модель ...]
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from hospitality.ai import orchestrator
from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.modules.guests import api as guests_api
from hospitality.platform.config import LocalizedText, load_tenant_config, store_tenant_config
from hospitality.platform.service_quotas import ServiceQuota, ServiceQuotaPaid
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

Kind = Literal["water_one", "water_two", "towels", "coffee"]

# Категории eval-тенанта bakeoff: housekeeping и room-service там есть.
_WATER = ServiceQuota(
    key="water",
    names=LocalizedText(en="Bottled water", ru="Бутилированная вода", kk="Ауыз су"),
    category_key="housekeeping",
    per_guest_per_day=20,
    paid=ServiceQuotaPaid(category_key="room-service", unit_price_minor=50_000),
)
_EXPECTED: dict[Kind, tuple[str | None, int | None]] = {
    "water_one": ("water", 1),
    "water_two": ("water", 2),
    "towels": (None, None),
    "coffee": (None, None),
}


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind
    message: str


_MESSAGES: dict[str, tuple[str, str, str, str]] = {
    "en": (
        "Could you bring some water, please?",
        "Please bring two bottles of water.",
        "Could I get fresh towels?",
        "Can you bring more coffee sachets?",
    ),
    "ru": (
        "Принесите, пожалуйста, воды.",
        "Принесите две бутылки воды.",
        "Принесите свежие полотенца.",
        "Принесите ещё кофе в пакетиках.",
    ),
    "kk": (
        "Су әкеліп беріңізші.",
        "Екі бөтелке су әкеліңізші.",
        "Таза сүлгі әкеліңізші.",
        "Тағы пакеттегі кофе әкеліңізші.",
    ),
    "zh": ("请送点水来。", "请送两瓶水来。", "请送干净的毛巾来。", "请再送些速溶咖啡包。"),
    "tr": (
        "Biraz su getirebilir misiniz?",
        "Lütfen iki şişe su getirin.",
        "Temiz havlu getirebilir misiniz?",
        "Biraz daha paket kahve getirir misiniz?",
    ),
    "hi": (
        "कृपया पानी ले आइए।",
        "कृपया पानी की दो बोतलें ले आइए।",
        "कृपया साफ़ तौलिये ले आइए।",
        "कृपया कुछ और कॉफ़ी के पाउच ले आइए।",
    ),
}
_KINDS: tuple[Kind, Kind, Kind, Kind] = ("water_one", "water_two", "towels", "coffee")

SCENARIOS: list[Scenario] = [
    Scenario(language, kind, message)
    for language, messages in _MESSAGES.items()
    for kind, message in zip(_KINDS, messages, strict=True)
]


_ROOM = "eval-quota"


async def _ensure_quota_tenant() -> uuid.UUID:
    """Eval-тенант bakeoff + квота воды + проживание на 20 гостей; id Stay.

    Квоту следующий прогон bakeoff снимет сам — он перезаписывает конфиг.
    """
    await _ensure_eval_tenant()
    tenant_id = await _eval_tenant_id()
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
        await store_tenant_config(
            session, tenant_id, config.model_copy(update={"service_quotas": (_WATER,)})
        )
    with tenant_context(tenant_id):
        stay = await guests_api.find_active_stay(_ROOM)
        if stay is None:
            checked = await guests_api.check_in(
                guests_api.StayCheckIn(
                    room_number=_ROOM,
                    check_out_at=utc_now() + timedelta(days=30),
                    guests_count=guests_api.MAX_GUESTS_PER_STAY,
                )
            )
            stay = checked.stay
    return stay.id


def _check(scenario: Scenario, turn: orchestrator.OrchestratorTurn) -> tuple[str, str]:
    """«OK», «!!» (ложный ключ, не то число, нет заявки) или «miss» (ключ пропущен)."""
    if turn.pending_action is None:
        return "!! ", f"заявки нет ({turn.kind.value}): {turn.reply_text[:90]!r}"
    arguments = turn.pending_action.arguments
    key, units = arguments.get("quota_key"), arguments.get("quota_units")
    detail = f"quota_key={key!r}, quota_units={units!r}, category={arguments.get('category_key')!r}"
    expected_key, expected_units = _EXPECTED[scenario.kind]
    if expected_key is None:
        return ("OK " if key is None else "!! "), detail
    if key is None:
        return "miss", detail
    return ("OK " if (key, units) == (expected_key, expected_units) else "!! "), detail


async def run(models: list[str], repeat: int) -> None:
    providers = _build_candidates(models)
    if not providers:
        print("Ни один кандидат не собран — причины выше.")
        return
    os.environ["ENABLE_SERVICE_REQUESTS"] = "true"
    get_settings.cache_clear()
    stay_id = await _ensure_quota_tenant()
    tenant_id = await _eval_tenant_id()
    failures = misses = 0
    for model, provider in providers.items():
        print(f"\n===== {model}, повторов: {repeat} =====")
        for scenario in SCENARIOS:
            for _ in range(repeat):
                try:
                    with tenant_context(tenant_id):
                        turn = await orchestrator.handle_message(
                            message=scenario.message,
                            provider=provider,
                            verified_room_number=_ROOM,
                            verified_stay_id=stay_id,
                        )
                    mark, detail = _check(scenario, turn)
                except AppError as error:
                    mark, detail = "!! ", f"ERROR {error.code}: {error.message}"
                failures += mark.startswith("!!")
                misses += mark == "miss"
                print(f"  {mark} [{scenario.language}/{scenario.kind}] {detail}")
    print(f"\nПровалов: {failures}; пропусков ключа: {misses}.")
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
