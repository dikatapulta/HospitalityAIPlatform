"""Логика AI Gateway (Task 0014, FOUNDATION §7.2).

Единственный путь поговорить с LLM: `complete()` внутри `tenant_context(...)`
(P-4). Порядок вызова: резерв бюджета → бюджет тенанта → до `LLM_MAX_ATTEMPTS`
попыток провайдера (ретрай только по таймауту, между попытками — пауза) →
стоимость по прайс-листу → строка `LlmCallLog` + событие `llm_call` в логах →
снятие резерва. Каждый исход — успех, исчерпанные таймауты, ошибка провайдера
— оставляет строку в журнале (DoD Task 0014).

Резерв и пауза — issue #46, ADR-017: без резерва бюджет видел только уже
записанные вызовы и параллельные ходы гостей проходили все разом (TOCTOU),
без паузы все попытки сгорали за доли секунды по лежащему провайдеру.

Маршрутизации моделей нет (Non-Goal): одна модель `LLM_MODEL`; она обязана
быть в прайс-листе, и это проверяется на старте процесса
(`validate_configured_model`, issue #137) — неизвестный id роняет деплой, а не
диалог гостя. Ожидаемые ошибки — `AppError` с кодами каталога
(docs/runbooks/errors.md, R-8).

Тот же дневной расход, по которому работает отказ, публикуется наружу
метриками (`refresh_budget_metrics`): исчерпание бюджета обязано быть видно
до того, как оно случится (issue #103, алерт ERR-OPS-006).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_UP, Decimal
from functools import lru_cache
from typing import Final

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.ai.gateway.anthropic_provider import AnthropicProvider
from hospitality.ai.gateway.models import LlmBudgetReservation, LlmCallLog, LlmCallStatus
from hospitality.ai.gateway.provider import (
    LlmProvider,
    LlmProviderError,
    LlmProviderResult,
    LlmProviderTimeoutError,
)
from hospitality.ai.gateway.schemas import LlmRequest, LlmResponse
from hospitality.platform.models import Tenant
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_llm_call, set_llm_daily_budget
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_AI_PROVIDER_TIMEOUT = "ERR-AI-001"
ERR_AI_BUDGET_EXCEEDED = "ERR-AI-002"
ERR_AI_PROVIDER_ERROR = "ERR-AI-003"

# Прайс-лист: $/1M токенов (input, output) по моделям. Единственное место
# истины для стоимости; модель вне прайс-листа — ошибка конфигурации, и процесс
# с ней не поднимается (`validate_configured_model`, issue #137): стоимость
# каждого вызова обязана считаться (§7.2).
# Кандидаты рантайма гостевого диалога (Task 0015) — Haiku 4.5 и Sonnet 5;
# финальный `LLM_MODEL` фиксируется bake-off'ом на 6 языках (spec 0015, §7.7).
# Sonnet 5 — стандартная цена $3/$15, НЕ интро $2/$10 (до 2026-08-31): COGS не
# должен занижаться молча после окончания интро-периода.
MODEL_PRICING_USD_PER_MTOK: Final[dict[str, tuple[Decimal, Decimal]]] = {
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}

_TOKENS_PER_MTOK = Decimal(1_000_000)
_CENT_FRACTION = Decimal("0.000001")  # шаг NUMERIC(12,6) — точность денег в БД

# Резерв бюджета (issue #46, ADR-017). Токенов входа заранее никто не знает,
# поэтому оценка идёт по длине сериализованного запроса: 2 символа на токен —
# пессимистично для латиницы (~4 символа на токен), близко к правде для
# кириллицы и, вероятно, занижает казахский, где токенов на тот же текст
# выходит больше. Занижение входа не делает резерв заниженным: его размер
# определяет ВЫХОД, который резервируется целиком по `max_tokens` (1024 токена
# у типового хода против ~300 фактических), и этот запас перекрывает
# недооценку входа с лихвой. Число «символов на токен» — инженерная оценка без
# документа-владельца: эта константа и есть его единственное место истины,
# ADR-017 объясняет почему (ADR-010 — про языки golden-set, плотности
# токенизации он не касается). Расходится с фактом — считать токенизатором
# («Пересмотреть» ADR-017).
_RESERVE_CHARS_PER_TOKEN = Decimal(2)
# Запас аренды поверх худшего вызова (все попытки + все паузы): резерв держится
# чуть дольше самого вызова, чтобы гонку не открыла собственная арифметика.
_RESERVATION_LEASE_MARGIN_SECONDS = 30.0


def validate_configured_model() -> None:
    """Fail-fast на старте процесса: `LLM_MODEL` обязана быть в прайс-листе (#137).

    Без этой проверки неизвестный идентификатор модели вскрывается только в
    `_compute_cost` — уже ПОСЛЕ ответа провайдера: гость получает 500 на первом
    же сообщении, вызов провайдеру оплачен, а строки в `llm_call_log` нет, и
    дневной бюджет слепнет ровно на ошибочных вызовах (аудит 21.07, H5).
    Типичный триггер — датированный id (`claude-sonnet-5-20250929`) или опечатка:
    ключи прайс-листа — идентификаторы без даты, и сверка строгая, по строке.

    Зовут её оба composition root'а — `app.py` и `worker.py`: образ кода один,
    конфигурация одна, и подняться с моделью вне прайс-листа не вправе ни один
    процесс. `SystemExit`, а не исключение, — канон конфигурационного отказа
    ядра (`shared/alerting.py`): человеку нужна строка «исправьте .env», а не
    трейсбек в crash-loop контейнера.
    """
    model = get_settings().llm_model
    if model in MODEL_PRICING_USD_PER_MTOK:
        return
    known = ", ".join(sorted(MODEL_PRICING_USD_PER_MTOK))
    raise SystemExit(
        f"LLM_MODEL={model!r} отсутствует в прайс-листе MODEL_PRICING_USD_PER_MTOK "
        "(src/hospitality/ai/gateway/service.py): стоимость каждого вызова обязана "
        f"считаться (FOUNDATION §7.2). Известные модели: {known}. "
        "Исправьте LLM_MODEL в .env или добавьте цену новой модели в прайс-лист."
    )


def _request_payload(request: LlmRequest) -> str:
    """Каноническая сериализация запроса — одна на хэш промпта и оценку резерва."""
    return request.model_dump_json(include={"system", "messages", "tools", "forced_tool"})


def compute_prompt_hash(request: LlmRequest) -> str:
    """sha256 канонической сериализации запроса — «версия промпта» (§7.2).

    Сериализация детерминирована (порядок полей модели фиксирован), сам текст
    промпта в журнал не пишется (PII, §7.6) — только хэш.
    """
    return hashlib.sha256(_request_payload(request).encode()).hexdigest()


def build_anthropic_provider(model: str) -> AnthropicProvider:
    """Боевой Anthropic-адаптер под конкретную модель.

    Ключ и таймаут — из настроек, модель — параметром: композиции нужен
    провайдер под `LLM_MODEL`, а bake-off'у (§7.7, spec 0015) — под каждого
    кандидата (Haiku 4.5 / Sonnet 5) поочерёдно, через ту же единственную дверь.
    """
    settings = get_settings()
    return AnthropicProvider(
        api_key=settings.anthropic_api_key,
        model=model,
        timeout_seconds=settings.llm_timeout_seconds,
    )


@lru_cache
def get_default_provider() -> AnthropicProvider:
    """Боевой провайдер из настроек окружения — синглтон, создаётся лениво."""
    return build_anthropic_provider(get_settings().llm_model)


async def complete(request: LlmRequest, *, provider: LlmProvider | None = None) -> LlmResponse:
    """Выполнить вызов LLM от имени текущего тенанта (канонический путь, P-12).

    `provider` переопределяется только в тестах (MockLlmProvider) и композиции;
    бизнес-код зовёт без него — боевой Anthropic из настроек.
    """
    if provider is None:
        provider = get_default_provider()
    settings = get_settings()
    prompt_hash = compute_prompt_hash(request)

    # Резерв — ДО проверки бюджета: иначе собственный вызов остаётся невидимым
    # для параллельных, и проверка снова считает только прошлое (ADR-017).
    reservation_id = await _reserve_budget(_reserve_usd(request), settings)
    try:
        await _ensure_tenant_budget(Decimal(str(settings.llm_tenant_daily_budget_usd)))

        max_attempts = settings.llm_max_attempts
        started_at = time.monotonic()
        result: LlmProviderResult | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                result = await provider.complete(request)
                break
            except LlmProviderTimeoutError:
                logger.warning(
                    "llm_call_timeout",
                    provider=provider.name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                if attempt == max_attempts:
                    await _log_call(
                        provider=provider.name,
                        model=settings.llm_model,
                        prompt_hash=prompt_hash,
                        status=LlmCallStatus.TIMEOUT,
                        latency_ms=_elapsed_ms(started_at),
                    )
                    raise AppError(
                        code=ERR_AI_PROVIDER_TIMEOUT,
                        message="LLM provider did not respond in time",
                        status_code=503,
                    ) from None
                # Пауза перед следующей попыткой (issue #46, ADR-017): без неё
                # все попытки сгорают за доли секунды по лежащему провайдеру.
                await _sleep_before_retry(attempt, settings)
            except LlmProviderError as error:
                await _log_call(
                    provider=provider.name,
                    model=settings.llm_model,
                    prompt_hash=prompt_hash,
                    status=LlmCallStatus.ERROR,
                    latency_ms=_elapsed_ms(started_at),
                )
                logger.warning("llm_call_failed", provider=provider.name, error=str(error))
                raise AppError(
                    code=ERR_AI_PROVIDER_ERROR,
                    message="LLM provider request failed",
                    status_code=502,
                ) from error
        # Цикл либо присвоил result, либо поднял AppError. Витка хотя бы один:
        # `llm_max_attempts` не бывает меньше 1 — границу держит `Field(ge=1)`
        # в shared/config.py, и негодная настройка не даёт процессу подняться
        # (issue #273), а не доживает до 500 у гостя.
        assert result is not None

        latency_ms = _elapsed_ms(started_at)
        cost_usd = _compute_cost(result)
        call_id = await _log_call(
            provider=provider.name,
            model=result.model,
            prompt_hash=prompt_hash,
            status=LlmCallStatus.OK,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
        logger.info(
            "llm_call",
            provider=provider.name,
            model=result.model,
            prompt_hash=prompt_hash,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=str(cost_usd),
            latency_ms=latency_ms,
        )
        return LlmResponse(
            call_id=call_id,
            text=result.text,
            model=result.model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            prompt_hash=prompt_hash,
            tool_calls=result.tool_calls,
            stop_reason=result.stop_reason,
        )
    finally:
        # Резерв снимается ПОСЛЕ записи исхода в журнал (все три исхода пишут
        # строку до выхода из try): между «резерва больше нет» и «расход виден»
        # не остаётся окна, в котором вызов невидим обеим проверкам.
        await _release_budget_reservation(reservation_id)


def _compute_cost(result: LlmProviderResult) -> Decimal:
    pricing = MODEL_PRICING_USD_PER_MTOK.get(result.model)
    if pricing is None:
        # Страховка на случай провайдера, собранного мимо настроек
        # (`build_anthropic_provider` с произвольной моделью — bake-off, §7.7):
        # у боевого пути эту ветку закрывает `validate_configured_model` на
        # старте. Ошибка конфигурации/программиста, не бизнес-ошибка: наружу — 500.
        raise ValueError(
            f"model {result.model!r} is missing from MODEL_PRICING_USD_PER_MTOK: "
            "стоимость каждого вызова обязана считаться (FOUNDATION 7.2)"
        )
    input_price, output_price = pricing
    return (
        Decimal(result.input_tokens) * input_price + Decimal(result.output_tokens) * output_price
    ) / _TOKENS_PER_MTOK


def _utc_day_start() -> datetime:
    """Начало текущих UTC-суток — окно, в котором считается дневной бюджет."""
    return utc_now().replace(hour=0, minute=0, second=0, microsecond=0)


async def _daily_spent_usd(session: AsyncSession) -> Decimal:
    """Сумма затрат ТЕКУЩЕГО тенанта за текущие UTC-сутки.

    Тенантная сессия — чужие затраты не видны и не считаются (RLS, P-4);
    поэтому же снимок по всем тенантам собирается циклом, а не одним
    группирующим запросом (``refresh_budget_metrics``). Сессия приходит
    аргументом: проверка бюджета читает расход и живые резервы одной
    транзакцией, а снимок метрик — только расход.
    """
    spent = await session.scalar(
        select(func.coalesce(func.sum(LlmCallLog.cost_usd), 0)).where(
            LlmCallLog.created_at >= _utc_day_start()
        )
    )
    return Decimal(spent if spent is not None else 0)


async def _live_reserved_usd(session: AsyncSession, now: datetime) -> Decimal:
    """Сумма НЕИСТЁКШИХ резервов текущего тенанта — вызовы, идущие прямо сейчас.

    Истёкшая аренда в сумму не входит: процесс, умерший между резервом и
    записью исхода, не имеет права держать бюджет отеля до полуночи (ADR-016,
    тот же приём, что `locked_until` у outbox).
    """
    reserved = await session.scalar(
        select(func.coalesce(func.sum(LlmBudgetReservation.amount_usd), 0)).where(
            LlmBudgetReservation.reserved_until > now
        )
    )
    return Decimal(reserved if reserved is not None else 0)


def _reserve_usd(request: LlmRequest) -> Decimal:
    """Во сколько худшем случае обойдётся ОДИН вызов этого запроса (ADR-017).

    Выход берётся целиком по `max_tokens` (больше модель вернуть не может),
    вход — оценкой по длине сериализованного запроса.

    Цена — из прайс-листа для `LLM_MODEL`, и о фактическом провайдере функция
    не знает ничего: порт `LlmProvider` отдаёт только `name`, модель появляется
    в `LlmProviderResult` уже ПОСЛЕ вызова. Поэтому провайдер, собранный мимо
    настроек (bake-off, §7.7), резервируется по цене `LLM_MODEL` и при более
    дорогом кандидате занижается — для ручного офлайн-прогона это допустимо
    (гостевой трафик всегда идёт моделью из настроек), а честная починка меняет
    контракт порта — issue #268.
    """
    input_price, output_price = _reserve_pricing()
    input_tokens = Decimal(len(_request_payload(request))) / _RESERVE_CHARS_PER_TOKEN
    total = (input_tokens * input_price + Decimal(request.max_tokens) * output_price) / (
        _TOKENS_PER_MTOK
    )
    # Округление вверх: резерв в ноль не схлопывается даже у самого короткого запроса.
    return total.quantize(_CENT_FRACTION, rounding=ROUND_UP)


def _reserve_pricing() -> tuple[Decimal, Decimal]:
    """Цена для резерва: строка прайс-листа под `LLM_MODEL`.

    Ветка `max(...)` — не про bake-off, а про то, что функция обязана вернуть
    число: `_compute_cost` с его `ValueError` здесь неуместен (исход вызова ещё
    неизвестен, ронять ход гостя не на чем). В боевом процессе она недостижима —
    `validate_configured_model()` (#137) не даёт подняться с моделью вне
    прайс-листа; сработать она может только там, где composition root'а нет
    (тесты, разовые скрипты), и тогда берёт самую дорогую строку, чтобы
    неизвестность не оборачивалась заниженным резервом.
    """
    pricing = MODEL_PRICING_USD_PER_MTOK.get(get_settings().llm_model)
    if pricing is not None:
        return pricing
    return (
        max(price for price, _ in MODEL_PRICING_USD_PER_MTOK.values()),
        max(price for _, price in MODEL_PRICING_USD_PER_MTOK.values()),
    )


def _reservation_lease_seconds(settings: Settings) -> float:
    """Срок аренды резерва — худший вызов целиком: все попытки, все паузы, запас."""
    attempts = settings.llm_max_attempts
    backoff_total = sum(_retry_delay_seconds(attempt, settings) for attempt in range(1, attempts))
    return (
        settings.llm_timeout_seconds * attempts + backoff_total + _RESERVATION_LEASE_MARGIN_SECONDS
    )


async def _reserve_budget(amount_usd: Decimal, settings: Settings) -> uuid.UUID:
    """Занять место в дневном бюджете на время вызова (issue #46, ADR-017)."""
    now = utc_now()
    reservation = LlmBudgetReservation(
        amount_usd=amount_usd,
        reserved_until=now + timedelta(seconds=_reservation_lease_seconds(settings)),
    )
    async with session_scope() as session:
        # Гигиена на своём же пути, без отдельной фоновой задачи (NG-8):
        # истёкшие резервы бюджет уже не держат, и место занимать им незачем.
        await session.execute(
            delete(LlmBudgetReservation).where(LlmBudgetReservation.reserved_until <= now)
        )
        session.add(reservation)
        await session.flush()
    return reservation.id


async def _release_budget_reservation(reservation_id: uuid.UUID) -> None:
    """Снять резерв: расход этого вызова уже записан в журнал (или его не было).

    Сбой снятия не имеет права подменить собой исход вызова — гость получил бы
    ошибку БД вместо ответа модели. Поэтому здесь только предупреждение:
    незакрытый резерв всё равно перестанет держать бюджет по истечении аренды.
    """
    try:
        async with session_scope() as session:
            await session.execute(
                delete(LlmBudgetReservation).where(LlmBudgetReservation.id == reservation_id)
            )
    except Exception:
        logger.warning("llm_budget_reservation_release_failed", exc_info=True)


async def refresh_budget_metrics() -> None:
    """Опубликовать в ``/metrics`` расход и лимит каждого тенанта за сутки (#103).

    Зовётся на каждый scrape ``/metrics`` (подключение — ``app.py``), а не по
    ходу вызовов LLM: значение обязано стареть вместе с сутками. Иначе после
    полуночи отель, который сегодня ещё не писал боту, показывал бы вчерашний
    расход, и алерт «бюджет на исходе» не гас бы до первого сообщения гостя.

    Запрос на тенанта, а не один с ``GROUP BY``: расход лежит в тенантной
    таблице под RLS, и платформенная сессия не видит там ни строки (ADR-003).
    Обойти это политикой уровня ``outbox_events`` нельзя — исключение
    диспетчера в бизнес-таблицы не копируется (``platform_session_scope``).
    Тенантов на инсталляции единицы, запрос индексный, scrape — раз в минуту;
    когда тенантов станут сотни, лечится счётчиком-агрегатом, а не изоляцией.
    """
    budget_usd = Decimal(str(get_settings().llm_tenant_daily_budget_usd))
    try:
        async with platform_session_scope() as session:
            tenant_ids = list(await session.scalars(select(Tenant.id)))
        snapshot = {}
        for tenant_id in tenant_ids:
            with tenant_context(tenant_id):
                async with session_scope() as session:
                    spent_usd = await _daily_spent_usd(session)
            snapshot[str(tenant_id)] = (spent_usd, budget_usd)
    except Exception:  # диагностический путь: /metrics обязан отдаваться и без БД
        logger.warning("llm_daily_spend_unavailable", exc_info=True)
        set_llm_daily_budget({})
        return
    set_llm_daily_budget(snapshot)


async def _ensure_tenant_budget(daily_budget_usd: Decimal) -> None:
    """Дневной бюджет тенанта: записанный расход ПЛЮС вызовы, идущие сейчас.

    Резервы в сумме — та самая половина, которой не было до issue #46: без них
    проверка видела только прошлое, и параллельные вызовы все читали «лимит не
    исчерпан» (TOCTOU, ADR-017). Свой резерв в сумму уже входит — он сделан до
    этой проверки, поэтому лимит не может быть перейдён и самим этим вызовом.
    """
    day_start = _utc_day_start()
    now = utc_now()
    async with session_scope() as session:
        spent_usd = await _daily_spent_usd(session)
        reserved_usd = await _live_reserved_usd(session, now)
    if spent_usd + reserved_usd > daily_budget_usd:
        logger.warning(
            "llm_budget_exceeded",
            spent_usd=str(spent_usd),
            reserved_usd=str(reserved_usd),
            daily_budget_usd=str(daily_budget_usd),
        )
        raise AppError(
            code=ERR_AI_BUDGET_EXCEEDED,
            message="Tenant daily LLM budget is exhausted",
            status_code=429,
            # Бюджет дневной — раньше начала следующих UTC-суток повторять нет смысла.
            headers={
                "Retry-After": str(int((day_start + timedelta(days=1) - utc_now()).total_seconds()))
            },
        )


async def _log_call(
    *,
    provider: str,
    model: str,
    prompt_hash: str,
    status: LlmCallStatus,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal = Decimal(0),
    latency_ms: int = 0,
) -> uuid.UUID:
    """Записать вызов в `LlmCallLog` — отдельной короткой транзакцией
    (сетевые вызовы провайдера не живут внутри транзакции БД)."""
    # Метрики §10.7 — та же единая точка всех исходов, что и журнал (Task 0018).
    record_llm_call(
        model=model,
        status=status.value,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    call = LlmCallLog(
        correlation_id=correlation_id if isinstance(correlation_id, str) else None,
        provider=provider,
        model=model,
        prompt_hash=prompt_hash,
        status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
    )
    async with session_scope() as session:
        session.add(call)
        await session.flush()
    return call.id


def _retry_delay_seconds(attempt: int, settings: Settings) -> float:
    """Верхняя граница паузы после `attempt`-й попытки (формула канона ADR-009).

    `база × 2^(попытка−1)` — та же экспонента, что у доставки outbox; потолком
    служит `LLM_TIMEOUT_SECONDS`, а не отдельная настройка: ждать дольше одной
    попытки на ходе гостя, который ждёт ответа прямо сейчас, бессмысленно.
    """
    delay = settings.llm_retry_backoff_base_seconds * float(2 ** (attempt - 1))
    return min(delay, settings.llm_timeout_seconds)


async def _sleep_before_retry(attempt: int, settings: Settings) -> None:
    """Подождать перед следующей попыткой — с разбросом (jitter).

    ADR-009 разброс не делал осознанно: воркер один, и синхронизироваться его
    повторам не с чем. У шлюза наоборот — вызывающих столько, сколько гостей
    пишет прямо сейчас, и авария провайдера обрывает их таймаутом почти
    одновременно. Фиксированная пауза собрала бы все повторы в одну волну по
    и без того лежащему провайдеру — ровно то, против чего пауза и заводится.
    Разброс половинный: пауза остаётся в [0.5×, 1×] от расчётной, то есть не
    исчезает и не растягивает ожидание гостя сверх посчитанного.
    """
    delay = _retry_delay_seconds(attempt, settings)
    await asyncio.sleep(delay * (0.5 + random.random() / 2))


def _elapsed_ms(started_at: float) -> int:
    return round((time.monotonic() - started_at) * 1000)
