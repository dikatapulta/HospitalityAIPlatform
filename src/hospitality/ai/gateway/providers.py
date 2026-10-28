"""Боевой провайдер под модель и проверка конфигурации на старте (ADR-020, ADR-024).

Провайдер выводится из модели по прайс-листу (`MODEL_PROVIDER`): одна
настройка модели переключает и модель, и адаптер. Отдельным файлом по
той же причине, что `price_list.py`: `service.py` и без этого за границей R-3.
"""

from __future__ import annotations

from functools import lru_cache

from hospitality.ai.gateway.anthropic_provider import AnthropicProvider
from hospitality.ai.gateway.openai_provider import OpenAIProvider
from hospitality.ai.gateway.price_list import (
    EMBEDDING_MODEL_PROVIDER,
    EMBEDDING_PRICING_USD_PER_MTOK,
    MODEL_PRICING_USD_PER_MTOK,
    MODEL_PROVIDER,
    STREAMING_PROVIDERS,
    ProviderName,
)
from hospitality.ai.gateway.provider import (
    EmbeddingProvider,
    LlmProvider,
    LlmStreamingProvider,
)
from hospitality.shared.config import Settings, get_settings


def build_provider(model: str) -> LlmProvider:
    """Боевой адаптер под конкретную модель — её провайдера по прайс-листу.

    Ключ и таймаут — из настроек, модель — параметром: композиции нужен
    провайдер под модель назначения, а bake-off'у (§7.7, spec 0015) — под каждого
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
        return _openai(model, settings)
    raise ValueError(
        f"model {model!r} is missing from MODEL_PRICING_USD_PER_MTOK: "
        "стоимость каждого вызова обязана считаться (FOUNDATION 7.2)"
    )


def build_streaming_provider(model: str) -> LlmStreamingProvider:
    """Боевой адаптер потока под модель (ADR-024 §5); провайдер без потока — `ValueError`."""
    provider = MODEL_PROVIDER.get(model)
    if provider == "openai":
        return _openai(model, get_settings())
    raise ValueError(f"model {model!r}: адаптер её провайдера не умеет поток (ADR-024 §5)")


def build_embedding_provider(model: str) -> EmbeddingProvider:
    """Боевой адаптер эмбеддингов под модель группы эмбеддингов прайс-листа (ADR-024 §3)."""
    if EMBEDDING_MODEL_PROVIDER.get(model) == "openai":
        return _openai(model, get_settings())
    raise ValueError(
        f"embedding model {model!r} is missing from EMBEDDING_PRICING_USD_PER_MTOK: "
        "стоимость каждого вызова обязана считаться (FOUNDATION 7.2)"
    )


def _openai(model: str, settings: Settings) -> OpenAIProvider:
    return OpenAIProvider(
        api_key=settings.openai_api_key,
        model=model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


@lru_cache
def provider_for_model(model: str) -> LlmProvider:
    """Боевой провайдер под модель назначения — по синглтону на модель, лениво."""
    return build_provider(model)


@lru_cache
def streaming_provider_for_model(model: str) -> LlmStreamingProvider:
    return build_streaming_provider(model)


@lru_cache
def embedding_provider_for_model(model: str) -> EmbeddingProvider:
    return build_embedding_provider(model)


def get_default_provider() -> LlmProvider:
    """Боевой провайдер гостевой модели `LLM_MODEL`."""
    return provider_for_model(get_settings().llm_model)


def validate_configured_model() -> None:
    """Fail-fast на старте процесса: модели назначений обслуживаемы и оплачиваемы (#137).

    Без этой проверки неизвестный идентификатор модели вскрывается только в
    `_compute_cost` — уже ПОСЛЕ ответа провайдера: гость получает 500 на первом
    же сообщении, вызов провайдеру оплачен, а строки в `llm_call_log` нет, и
    дневной бюджет слепнет ровно на ошибочных вызовах (аудит 21.07, H5).
    Типичный триггер — датированный id (`claude-sonnet-5-20250929`) или опечатка:
    ключи прайс-листа — идентификаторы без даты, и сверка строгая, по строке.

    Модель OpenAI требует ещё и `OPENAI_API_KEY` (ADR-020 §7): её выбирают только
    намеренно, и выбранная без ключа — заведомо ошибка конфигурации. У Anthropic
    пустой ключ по-прежнему валиден — это штатный режим dev/CI на Mock-провайдере.

    `LLM_MODEL_MENTOR` пуст — назначение `mentor` выключено, проверять нечего.
    Задан — модель обязана быть в прайс-листе чатов, её адаптер — уметь поток, ключ
    её провайдера — быть задан; то же для `LLM_EMBEDDING_MODEL` (ADR-024 §2).

    Зовут её оба composition root'а — `app.py` и `worker.py` (через
    `preflight.py`): образ кода один, конфигурация одна, и подняться с негодной
    моделью не вправе ни один процесс. `SystemExit`, а не исключение, — канон
    конфигурационного отказа ядра (`shared/alerting.py`): человеку нужна строка
    «исправьте .env», а не трейсбек в crash-loop контейнера.
    """
    settings = get_settings()
    _validate_chat_model("LLM_MODEL", settings.llm_model, settings)
    if not settings.llm_model_mentor:
        return
    provider = _validate_chat_model("LLM_MODEL_MENTOR", settings.llm_model_mentor, settings)
    if provider not in STREAMING_PROVIDERS:
        raise SystemExit(
            f"LLM_MODEL_MENTOR={settings.llm_model_mentor!r}: адаптер провайдера {provider!r} "
            "не умеет поток, а «Наставник» отвечает потоком (ADR-024 §5). Выберите модель "
            "OpenAI из прайс-листа или оставьте LLM_MODEL_MENTOR пустым."
        )
    embedding_model = settings.llm_embedding_model
    embedding_provider = EMBEDDING_MODEL_PROVIDER.get(embedding_model)
    if embedding_provider is None:
        known = ", ".join(sorted(EMBEDDING_PRICING_USD_PER_MTOK))
        raise SystemExit(
            f"LLM_EMBEDDING_MODEL={embedding_model!r} отсутствует в группе эмбеддингов "
            "прайс-листа (src/hospitality/ai/gateway/price_list.py). "
            f"Известные модели: {known}."
        )
    _require_key("LLM_EMBEDDING_MODEL", embedding_model, embedding_provider, settings)


def _validate_chat_model(setting: str, model: str, settings: Settings) -> ProviderName:
    provider = MODEL_PROVIDER.get(model)
    if provider is None:
        known = ", ".join(sorted(MODEL_PRICING_USD_PER_MTOK))
        raise SystemExit(
            f"{setting}={model!r} отсутствует в прайс-листе MODEL_PRICING_USD_PER_MTOK "
            "(src/hospitality/ai/gateway/price_list.py): стоимость каждого вызова обязана "
            f"считаться (FOUNDATION §7.2). Известные модели: {known}. "
            f"Исправьте {setting} в .env или добавьте цену новой модели в прайс-лист."
        )
    _require_key(setting, model, provider, settings)
    return provider


def _require_key(setting: str, model: str, provider: ProviderName, settings: Settings) -> None:
    if provider == "openai" and not settings.openai_api_key:
        raise SystemExit(
            f"{setting}={model!r} обслуживает OpenAI, а OPENAI_API_KEY не задан "
            "(docs/runbooks/secrets.md): первый же вызов упал бы ошибкой. "
            "Задайте ключ в .env или верните модель Anthropic (ADR-020)."
        )
