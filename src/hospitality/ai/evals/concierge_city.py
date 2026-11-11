"""Evals блока «Weather and exchange rates» на языках пилота (spec 0084 §5, FOUNDATION §7.7).

Что мерим — что слышит гость, когда бот видит погоду и официальный курс:
- `weather` — «какая погода завтра?»: бот называет источник — MET Norway;
- `rate` — «какой курс доллара?»: число из блока (447,52);
- `bill` — «мой счёт 85 000 тенге — сколько в долларах?»: счёт не пересчитан
  (85 000 / 447,52 ≈ 190 — этого числа в ответе нет);
- `unasked` — «во сколько завтрак?»: о погоде и курсе бот молчит.

Машинно — эскалация вместо ответа, язык (кириллица у гостя en/zh/tr/hi) и признак
сценария выше. «Официальный, у обменников свой», краткость и тон оценивает человек
по печати. Режим — «только консультации», как на гостевой копии.

Погоду и курс подставляют порты-заглушки `_FixedWeather` и `_FixedRates`: адаптеры
из `ai/` не импортируются (контракт 2 import-linter). Конфиг eval-тенанта получает
`concierge_city` — следующий прогон bakeoff его перезапишет. Запуск ПЛАТНЫЙ (24 хода
модели гостя), только с согласия основателя (CLAUDE.md); Postgres и ключ — как у
bakeoff:

    python -m hospitality.ai.evals.concierge_city [--repeat N] [модель ...]
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from hospitality.ai import orchestrator
from hospitality.ai.evals.bakeoff import _build_candidates, _ensure_eval_tenant, _eval_tenant_id
from hospitality.platform.config import ConciergeCity, load_tenant_config, store_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.ports.exchange_rates import OfficialRate, OfficialRates, set_exchange_rates
from hospitality.shared.ports.weather import (
    GeoPoint,
    WeatherCondition,
    WeatherForecast,
    WeatherHour,
    set_weather,
)
from hospitality.shared.tenancy import tenant_context

Kind = Literal["weather", "rate", "bill", "unasked"]
_KINDS: tuple[Kind, ...] = ("weather", "rate", "bill", "unasked")


@dataclass(frozen=True)
class Scenario:
    language: str
    kind: Kind
    message: str


_MESSAGES: dict[str, tuple[str, str, str, str]] = {
    "en": (
        "What will the weather be like tomorrow?",
        "What's the US dollar exchange rate today?",
        "My bill is 85,000 tenge. How much is that in dollars?",
        "What time is breakfast?",
    ),
    "ru": (
        "Какая погода будет завтра?",
        "Какой сегодня курс доллара?",
        "Мой счёт — 85 000 тенге. Сколько это в долларах?",
        "Во сколько завтрак?",
    ),
    "kk": (
        "Ертең ауа райы қандай болады?",
        "Бүгін доллар бағамы қандай?",
        "Менің шотым — 85 000 теңге. Бұл долларға шаққанда қанша болады?",
        "Таңғы ас сағат нешеде?",
    ),
    "zh": (
        "明天天气怎么样？",
        "今天美元汇率是多少？",
        "我的账单是85,000坚戈。换算成美元是多少？",
        "早餐几点开始？",
    ),
    "tr": (
        "Yarın hava nasıl olacak?",
        "Bugün dolar kuru ne kadar?",
        "Faturam 85.000 tenge. Bu kaç dolar eder?",
        "Kahvaltı saat kaçta?",
    ),
    "hi": (
        "कल मौसम कैसा रहेगा?",
        "आज डॉलर की विनिमय दर क्या है?",
        "मेरा बिल 85,000 टेंगे है। यह कितने डॉलर होगा?",
        "नाश्ता कितने बजे है?",
    ),
}

SCENARIOS: list[Scenario] = [
    Scenario(language, kind, message)
    for language, messages in _MESSAGES.items()
    for kind, message in zip(_KINDS, messages, strict=True)
]

EVAL_CITY = ConciergeCity(name="Almaty", latitude=43.2389, longitude=76.8897)
_USD_RATE = Decimal("447.52")
# Гость этих языков кириллицу читать не обязан: она в ответе — чужой язык (#342).
_NON_CYRILLIC = ("en", "zh", "tr", "hi")
# 85 000 / 447,52 = 189,93: пересчитанный счёт.
_BILL_IN_DOLLARS = ("189", "190")
_BLOCK_MARKS = ("°", "MET", "447")


class _FixedWeather:
    """Порт погоды на прогон: 48 часов от текущего, днём до 4 °C, облачно."""

    backend = "eval"

    async def forecast(self, point: GeoPoint) -> WeatherForecast:
        start = utc_now().replace(minute=0, second=0, microsecond=0)
        return WeatherForecast(
            updated_at=start,
            hours=tuple(
                WeatherHour(
                    starts_at=start + timedelta(hours=offset),
                    air_temperature_c=float(offset % 24 // 4 - 2),
                    wind_speed_ms=3.0,
                    condition=WeatherCondition.CLOUDY,
                )
                for offset in range(48)
            ),
        )

    async def check(self) -> bool:
        return True


class _FixedRates:
    """Порт курсов на прогон: опубликованы на сам день."""

    backend = "eval"

    async def official_rates(self, on: date) -> OfficialRates:
        return OfficialRates(
            "KZT",
            on,
            (
                OfficialRate("USD", 1, _USD_RATE),
                OfficialRate("EUR", 1, Decimal("502.21")),
                OfficialRate("RUB", 1, Decimal("5.41")),
            ),
        )

    async def check(self) -> bool:
        return True


async def _ensure_concierge_tenant() -> None:
    """Eval-тенант bakeoff с городом погоды; порты — заглушки прогона."""
    await _ensure_eval_tenant()
    tenant_id = await _eval_tenant_id()
    async with platform_session_scope() as session:
        config = await load_tenant_config(session, tenant_id)
        await store_tenant_config(
            session, tenant_id, config.model_copy(update={"concierge_city": EVAL_CITY})
        )
    set_weather(_FixedWeather())
    set_exchange_rates(_FixedRates())


def _check(scenario: Scenario, turn: orchestrator.OrchestratorTurn) -> tuple[bool, str]:
    reply = turn.reply_text
    if turn.kind is orchestrator.TurnKind.NEEDS_HUMAN:
        return False, f"эскалация вместо ответа: {reply[:90]!r}"
    if scenario.language in _NON_CYRILLIC and any("а" <= c.lower() <= "я" for c in reply):
        return False, f"кириллица у гостя {scenario.language}: {reply[:90]!r}"
    if scenario.kind == "weather" and "MET" not in reply:
        return False, f"источник прогноза не назван: {reply[:90]!r}"
    if scenario.kind == "rate" and "447" not in reply:
        return False, f"курса из блока нет: {reply[:90]!r}"
    if scenario.kind == "bill" and any(sum_ in reply for sum_ in _BILL_IN_DOLLARS):
        return False, f"счёт пересчитан по курсу: {reply[:90]!r}"
    if scenario.kind == "unasked" and any(mark in reply for mark in _BLOCK_MARKS):
        return False, f"о погоде или курсе без вопроса: {reply[:90]!r}"
    return True, reply


async def run(models: list[str], repeat: int) -> None:
    providers = _build_candidates(models)
    if not providers:
        print("Ни один кандидат не собран — причины выше.")
        return
    os.environ["ENABLE_SERVICE_REQUESTS"] = "false"
    os.environ["ENABLE_CONCIERGE_CITY"] = "true"
    get_settings.cache_clear()
    await _ensure_concierge_tenant()
    tenant_id = await _eval_tenant_id()
    failures = 0
    for model, provider in providers.items():
        print(f"\n===== {model}, повторов: {repeat}; {utc_now():%d.%m %H:%M} UTC =====")
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
    print(f"\nПровалов: {failures}. Ответы — прочесть глазами (spec 0084 §5).")
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
