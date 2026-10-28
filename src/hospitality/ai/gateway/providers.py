"""Боевой провайдер под модель и проверка конфигурации на старте (ADR-020).

Провайдер выводится из модели по прайс-листу (`MODEL_PROVIDER`): одна
настройка `LLM_MODEL` переключает и модель, и адаптер. Отдельным файлом по
той же причине, что `price_list.py`: `service.py` и без этого за границей R-3.
"""

from __future__ import annotations

from functools import lru_cache

from hospitality.ai.gateway.anthropic_provider import AnthropicProvider
from hospitality.ai.gateway.openai_provider import OpenAIProvider
from hospitality.ai.gateway.price_list import MODEL_PRICING_USD_PER_MTOK, MODEL_PROVIDER
from hospitality.ai.gateway.provider import LlmProvider
from hospitality.shared.config import get_settings


def build_provider(model: str) -> LlmProvider:
    """Боевой адаптер под конкретную модель — её провайдера по прайс-листу.

    Ключ и таймаут — из настроек, модель — параметром: композиции нужен
    провайдер под `LLM_MODEL`, а bake-off'у (§7.7, spec 0015) — под каждого
    кандидата поочерёдно, через ту же единственную дверь. Модель вне
    прайс-листа и пустой ключ её провайдера — `ValueError`.
    """
    settings = get_settings()
    provider = MODEL_PROVIDER.get(model)
    if provider == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            model=model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if provider == "openai":
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            model=model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    raise ValueError(
        f"model {model!r} is missing from MODEL_PRICING_USD_PER_MTOK: "
        "стоимость каждого вызова обязана считаться (FOUNDATION 7.2)"
    )


@lru_cache
def get_default_provider() -> LlmProvider:
    """Боевой провайдер из настроек окружения — синглтон, создаётся лениво."""
    return build_provider(get_settings().llm_model)


def validate_configured_model() -> None:
    """Fail-fast на старте процесса: `LLM_MODEL` обслуживаема и оплачиваема (#137).

    Без этой проверки неизвестный идентификатор модели вскрывается только в
    `_compute_cost` — уже ПОСЛЕ ответа провайдера: гость получает 500 на первом
    же сообщении, вызов провайдеру оплачен, а строки в `llm_call_log` нет, и
    дневной бюджет слепнет ровно на ошибочных вызовах (аудит 21.07, H5).
    Типичный триггер — датированный id (`claude-sonnet-5-20250929`) или опечатка:
    ключи прайс-листа — идентификаторы без даты, и сверка строгая, по строке.

    Модель OpenAI требует ещё и `OPENAI_API_KEY` (ADR-020 §7): её выбирают только
    намеренно, и выбранная без ключа — заведомо ошибка конфигурации. У Anthropic
    пустой ключ по-прежнему валиден — это штатный режим dev/CI на Mock-провайдере.

    Зовут её оба composition root'а — `app.py` и `worker.py` (через
    `preflight.py`): образ кода один, конфигурация одна, и подняться с негодной
    моделью не вправе ни один процесс. `SystemExit`, а не исключение, — канон
    конфигурационного отказа ядра (`shared/alerting.py`): человеку нужна строка
    «исправьте .env», а не трейсбек в crash-loop контейнера.
    """
    settings = get_settings()
    model = settings.llm_model
    provider = MODEL_PROVIDER.get(model)
    if provider is None:
        known = ", ".join(sorted(MODEL_PRICING_USD_PER_MTOK))
        raise SystemExit(
            f"LLM_MODEL={model!r} отсутствует в прайс-листе MODEL_PRICING_USD_PER_MTOK "
            "(src/hospitality/ai/gateway/price_list.py): стоимость каждого вызова обязана "
            f"считаться (FOUNDATION §7.2). Известные модели: {known}. "
            "Исправьте LLM_MODEL в .env или добавьте цену новой модели в прайс-лист."
        )
    if provider == "openai" and not settings.openai_api_key:
        raise SystemExit(
            f"LLM_MODEL={model!r} обслуживает OpenAI, а OPENAI_API_KEY не задан "
            "(docs/runbooks/secrets.md): гость получил бы ошибку на первом же сообщении. "
            "Задайте ключ в .env или верните модель Anthropic (ADR-020)."
        )
