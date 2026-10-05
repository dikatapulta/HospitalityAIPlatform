"""Прайс-лист шлюза — реестр моделей: кто обслуживает модель и почём (§7.2).

Единственное место истины для стоимости вызова и для того, с какой моделью
процесс вправе стартовать (`validate_configured_model`, issue #137). Строки
сгруппированы по провайдеру: `LLM_MODEL` выбирает модель, а с ней и адаптер —
отдельной настройки провайдера нет, и разойтись им не с чем (ADR-020).

Отдельным файлом, а не в `service.py`: таблицу читают и учёт стоимости
(`service.py`), и сборка провайдера (`providers.py`), а `service.py` и без неё
за границей R-3.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final, Literal

ProviderName = Literal["anthropic", "openai"]

# $/1M токенов (input, output). Модель вне таблицы — ошибка конфигурации, и
# процесс с ней не поднимается: стоимость каждого вызова обязана считаться.
# Меняешь строку — сверяй со страницей цен её провайдера и обновляй дату.
_PRICE_LIST: Final[dict[ProviderName, dict[str, tuple[Decimal, Decimal]]]] = {
    # https://platform.claude.com/docs/en/about-claude/pricing, сверено 05.10.2026.
    # Кандидаты рантайма гостевого диалога (Task 0015) — Haiku 4.5 и Sonnet 5;
    # модель гостя — Sonnet 5 (ADR-012). Sonnet 5 стоит $2/$10: интро-цена стала
    # постоянной, повышение до $3/$15 с 01.09.2026 отменено (issue #348; до него
    # здесь стояло $3/$15: расход завышен в 1,5 раза, потолок $10 срабатывал на ≈$6,67 по счёту).
    "anthropic": {
        "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
        "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
        "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
    },
    # https://developers.openai.com/api/docs/pricing, сверено 05.10.2026 (ADR-020).
    # Кандидаты bake-off'а против Sonnet 5; гостей не обслуживают до #373.
    # Цены — для запросов до 272K токенов входа: длиннее у OpenAI вдвое дороже,
    # но ход гостя на два порядка короче. Модель группы обязана поддерживать
    # `reasoning.effort: "none"` — адаптер шлёт его всегда (ADR-020 §4).
    "openai": {
        "gpt-6-luna": (Decimal("0.10"), Decimal("0.50")),
        "gpt-5.6-luna": (Decimal("0.20"), Decimal("1.20")),
    },
}

MODEL_PRICING_USD_PER_MTOK: Final[dict[str, tuple[Decimal, Decimal]]] = {
    model: price for models in _PRICE_LIST.values() for model, price in models.items()
}
MODEL_PROVIDER: Final[dict[str, ProviderName]] = {
    model: provider for provider, models in _PRICE_LIST.items() for model in models
}
