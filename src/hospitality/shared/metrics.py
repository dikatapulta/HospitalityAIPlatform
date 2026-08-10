"""Метрики Prometheus-формата (Task 0018, FOUNDATION §10.7).

Единственное место объявления метрик платформы и роутер ``GET /metrics``.
Prometheus-сервера в Phase 0 нет (без Grafana-стека): формат стандартный,
потребители сегодня — алертер (``tools/alerter.py``) и curl, завтра — любой
managed-scraper.

- RED по эндпоинтам: ``record_http_request()`` вызывает
  ``CorrelationIdMiddleware`` — тот же ``finally``, что пишет ``http_request``.
  Лейбл ``route`` — шаблон маршрута (``/api/v1/requests/{request_id}``), не
  сырой путь: сырой путь взрывает кардинальность (UUID, сканеры); немэтчнутые
  запросы собираются под ``unmatched``. Статус — класс ``2xx/4xx/5xx``:
  алертеру нужен класс, точный код есть в логах ``http_request``.
- LLM: ``record_llm_call()`` вызывается из ``ai/gateway/service._log_call`` —
  единой точки всех исходов. Лейбл ``tenant_id`` — требование §10.7
  («по тенантам»); тенант берётся из контекста, как в ``events.publish`` (P-4).
- Дневной расход и лимит LLM по тенантам (``llm_daily_spend_usd`` /
  ``llm_daily_budget_usd``, issue #103) — снимок текущих UTC-суток, тот самый,
  по которому gateway отказывает на ERR-AI-002. Считает его владелец данных
  (``ai/gateway``) и кладёт сюда через ``set_llm_daily_budget()``: kernel
  импортировать ``ai/`` не вправе (R-5), поэтому зовётся он реестром
  обновляторов ниже, а подключается композиционным корнем.
- Глубина outbox, число похороненных событий и возраст пульса воркера
  (issue #136) считаются в момент scrape запросом к БД через
  ``platform_session_scope`` (outbox — кросс-тенантная таблица, как в
  ``deliver_pending_events``; пульс — нетенантная). Недоступная БД не роняет
  ``/metrics`` (алертер обязан продолжать читать счётчики 5xx): gauge = NaN
  + WARNING.

``/metrics`` анонимен — явное решение (§11), симметрично ``/health/*``:
PII и секретов в метриках нет; токен для алертера — лишняя связность Phase 0.
Пересмотр при выходе в прод — «Известные отступления» README ``shared``.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select

from hospitality.shared.db import platform_session_scope
from hospitality.shared.events import OutboxEvent
from hospitality.shared.heartbeat import read_heartbeat_age_seconds
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id_or_none

logger = get_logger(module=__name__)

# Лейбл route для запросов, не совпавших ни с одним маршрутом (404 сканеров):
# след сканирования виден, кардинальность ограничена одной константой.
UNMATCHED_ROUTE = "unmatched"

http_requests_total = Counter(
    "http_requests_total",
    "HTTP-запросы по маршрутам (RED, FOUNDATION §10.7)",
    labelnames=("method", "route", "status"),
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "Длительность HTTP-запросов по маршрутам (RED, FOUNDATION §10.7)",
    labelnames=("method", "route"),
)
llm_calls_total = Counter(
    "llm_calls_total",
    "Вызовы LLM через ai/gateway по исходам (FOUNDATION §10.7)",
    labelnames=("tenant_id", "model", "status"),
)
llm_tokens_total = Counter(
    "llm_tokens_total",
    "Токены LLM по направлению input/output (FOUNDATION §10.7)",
    labelnames=("tenant_id", "model", "direction"),
)
llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Стоимость вызовов LLM в USD (FOUNDATION §10.7)",
    labelnames=("tenant_id", "model"),
)
guest_rate_limited_total = Counter(
    "guest_rate_limited_total",
    "Отклонённые rate-limit'ом сообщения гостевого чата (issue #41, spec 0023)",
    labelnames=("tenant_id", "scope"),
)
guest_web_sessions_total = Counter(
    "guest_web_sessions_total",
    "Привязки веб-чата (spec 0027/0033): started | rejected — по коду; "
    "bind_started | bind_rejected — по QR-ссылке (доля путей — метрика §9)",
    labelnames=("tenant_id", "outcome"),
)
guest_consent_total = Counter(
    "guest_consent_total",
    "Consent-gate гостевого канала (spec 0029): prompted | granted",
    labelnames=("tenant_id", "outcome"),
)
staff_logins_total = Counter(
    "staff_logins_total",
    "Попытки входа в кабинет персонала (spec 0033 §9): ok | rejected | rate_limited",
    labelnames=("outcome",),
)
staff_checkins_total = Counter(
    "staff_checkins_total",
    "Заселения со страницы кабинета (spec 0033 §9)",
    labelnames=("tenant_id",),
)
outbox_pending_events = Gauge(
    "outbox_pending_events",
    "События outbox в очереди на доставку (не доставлены и не похоронены); NaN — БД недоступна",
)
outbox_dead_letter_events = Gauge(
    "outbox_dead_letter_events",
    "События outbox, исчерпавшие повторы доставки (dead_lettered_at IS NOT NULL, "
    "issue #133); NaN — БД недоступна",
)
worker_heartbeat_age_seconds = Gauge(
    "worker_heartbeat_age_seconds",
    "Секунд с последнего пульса воркера событий (issue #136); NaN — БД недоступна "
    "или строки пульса нет",
)
llm_daily_spend_usd = Gauge(
    "llm_daily_spend_usd",
    "Расход LLM тенанта за текущие UTC-сутки в USD — то, что сравнивает с лимитом "
    "бюджет gateway (issue #103)",
    labelnames=("tenant_id",),
)
llm_daily_budget_usd = Gauge(
    "llm_daily_budget_usd",
    "Дневной лимит LLM тенанта в USD, на котором вызовы начнут отвергаться "
    "(ERR-AI-002, issue #103)",
    labelnames=("tenant_id",),
)

router = APIRouter(tags=["metrics"])

# Функции, которые `/metrics` зовёт перед выдачей, чтобы обновить свои метрики
# в момент scrape. Существуют потому, что расход тенанта живёт за границей слоя:
# `llm_call_log` принадлежит `ai/gateway`, а kernel импортировать его не вправе
# (R-5). Обратная зависимость вместо прямой: считает тот, кто владеет данными,
# а подключает композиционный корень (`app.py` — единственный, кому видны все
# слои). Ключ словаря — имя: `create_app()` в тестах зовётся многократно, и
# список копил бы один и тот же обновлятор.
ScrapeRefresher = Callable[[], Awaitable[None]]
_scrape_refreshers: dict[str, ScrapeRefresher] = {}


def register_scrape_refresher(name: str, refresher: ScrapeRefresher) -> None:
    """Подключить обновлятор метрик, вызываемый на каждый scrape `/metrics`."""
    _scrape_refreshers[name] = refresher


def record_http_request(
    *, method: str, route: str, status_code: int, duration_seconds: float
) -> None:
    """Учесть завершённый HTTP-запрос (зовёт CorrelationIdMiddleware)."""
    status_class = f"{status_code // 100}xx"
    http_requests_total.labels(method=method, route=route, status=status_class).inc()
    http_request_duration_seconds.labels(method=method, route=route).observe(duration_seconds)


def record_llm_call(
    *,
    model: str,
    status: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: Decimal = Decimal(0),
) -> None:
    """Учесть вызов LLM любого исхода (зовёт ai/gateway/service._log_call).

    Тенант — из контекста (P-4, как ``events.publish``); вне контекста тенанта
    (недостижимо для боевого gateway) — лейбл ``none``, а не падение: метрики
    не имеют права ломать бизнес-путь.
    """
    tenant_id = current_tenant_id_or_none()
    tenant_label = str(tenant_id) if tenant_id is not None else "none"
    llm_calls_total.labels(tenant_id=tenant_label, model=model, status=status).inc()
    if input_tokens:
        llm_tokens_total.labels(tenant_id=tenant_label, model=model, direction="input").inc(
            input_tokens
        )
    if output_tokens:
        llm_tokens_total.labels(tenant_id=tenant_label, model=model, direction="output").inc(
            output_tokens
        )
    if cost_usd:
        llm_cost_usd_total.labels(tenant_id=tenant_label, model=model).inc(float(cost_usd))


def record_guest_rate_limited(scope: str) -> None:
    """Учесть отклонённое лимитом сообщение гостя (зовёт channels/…/guest.py).

    Тенант — из контекста (P-4, как ``record_llm_call``): канал ставит его до
    проверки лимита; ``none`` — оборонительный фолбэк, метрики не падают.
    """
    tenant_id = current_tenant_id_or_none()
    tenant_label = str(tenant_id) if tenant_id is not None else "none"
    guest_rate_limited_total.labels(tenant_id=tenant_label, scope=scope).inc()


def record_guest_web_session(outcome: str) -> None:
    """Учесть исход привязки веб-чата: started | rejected (код, spec 0027),
    bind_started | bind_rejected (QR-ссылка, spec 0033 §6).

    Тенант — из контекста (P-4), как ``record_guest_rate_limited``.
    """
    tenant_id = current_tenant_id_or_none()
    tenant_label = str(tenant_id) if tenant_id is not None else "none"
    guest_web_sessions_total.labels(tenant_id=tenant_label, outcome=outcome).inc()


def record_guest_consent(outcome: str) -> None:
    """Учесть шаг consent-gate (spec 0029): prompted | granted.

    Пара «сколько экранов показали / сколько согласий получили» — она же
    метрика доли ботов-сканеров: они экран получают, кнопку не жмут.
    Тенант — из контекста (P-4), как ``record_guest_web_session``.
    """
    tenant_id = current_tenant_id_or_none()
    tenant_label = str(tenant_id) if tenant_id is not None else "none"
    guest_consent_total.labels(tenant_id=tenant_label, outcome=outcome).inc()


def record_staff_login(outcome: str) -> None:
    """Учесть попытку входа в кабинет (зовёт platform/staff_auth.login).

    Без лейбла тенанта: login — платформенная операция, тенант на этом шаге
    ещё неизвестен (сессия принадлежит User, ADR-008 §1). Метрика «персонал
    предпочитает кабинет» (spec 0033 §9) добирается счётчиками действий в
    PR D серии.
    """
    staff_logins_total.labels(outcome=outcome).inc()


def record_staff_checkin() -> None:
    """Учесть заселение из кабинета (зовёт staff_portal, spec 0033 §9).

    Тенант — из контекста (P-4), как ``record_guest_web_session``.
    """
    tenant_id = current_tenant_id_or_none()
    tenant_label = str(tenant_id) if tenant_id is not None else "none"
    staff_checkins_total.labels(tenant_id=tenant_label).inc()


def set_llm_daily_budget(snapshot: Mapping[str, tuple[Decimal, Decimal]]) -> None:
    """Заменить снимок «расход/лимит за сутки» целиком: тенант → (расход, лимит).

    Именно ЗАМЕНИТЬ, а не дописать: снимок относится к текущим UTC-суткам, и
    прошлые значения после смены суток — вчерашние. Пустой снимок (расход
    посчитать не удалось) стирает обе метрики — для watchdog'а это «не знаю»,
    и он молчит, как на NaN у gauge без лейблов.

    Пара публикуется вместе, потому что вместе и читается: доля от лимита
    имеет смысл, только если оба числа из одного снимка.
    """
    llm_daily_spend_usd.clear()
    llm_daily_budget_usd.clear()
    for tenant_label, (spent_usd, budget_usd) in snapshot.items():
        llm_daily_spend_usd.labels(tenant_id=tenant_label).set(float(spent_usd))
        llm_daily_budget_usd.labels(tenant_id=tenant_label).set(float(budget_usd))


async def _refresh_outbox_depth() -> None:
    """Обе метрики outbox — одним запросом: очередь и dead-letter (issue #133).

    Похороненное событие из очереди выбывает: считать его «недоставленным»
    вечно значило бы, что `outbox_pending_events` не вернётся к нулю никогда —
    метрика глубины перестала бы быть сигналом «воркер не успевает».
    """
    try:
        async with platform_session_scope() as session:
            row = (
                await session.execute(
                    select(
                        func.count()
                        .filter(
                            OutboxEvent.processed_at.is_(None),
                            OutboxEvent.dead_lettered_at.is_(None),
                        )
                        .label("pending"),
                        func.count()
                        .filter(OutboxEvent.dead_lettered_at.is_not(None))
                        .label("dead_lettered"),
                    ).select_from(OutboxEvent)
                )
            ).one()
        outbox_pending_events.set(row.pending)
        outbox_dead_letter_events.set(row.dead_lettered)
    except Exception:  # диагностический путь: метрики живут, когда БД мертва
        logger.warning("outbox_depth_unavailable", exc_info=True)
        outbox_pending_events.set(math.nan)
        outbox_dead_letter_events.set(math.nan)


async def _refresh_worker_heartbeat_age() -> None:
    """Возраст пульса воркера (issue #136) — тем же приёмом, что глубина outbox.

    Считает НЕ приложение о себе: строку пишет процесс воркера
    (`shared/heartbeat.py`), а `/metrics` лишь показывает её возраст наружу —
    watchdog'у (`tools/alerter.py`), у которого нет доступа к БД. Отдельный
    запрос по первичному ключу, а не ещё одно условие к запросу глубины: у
    него другая таблица, и складывать их в один `count(*) FILTER` без
    ограничивающего `WHERE` — ровно приём из issue #257.

    NaN — «не знаю» (БД недоступна или строки пульса нет): watchdog на нём
    молчит, а недоступная БД уже покрыта ERR-OPS-001.
    """
    try:
        age = await read_heartbeat_age_seconds()
    except Exception:  # диагностический путь: метрики живут, когда БД мертва
        logger.warning("worker_heartbeat_age_unavailable", exc_info=True)
        age = None
    worker_heartbeat_age_seconds.set(math.nan if age is None else age)


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """Выдача всех метрик процесса в текстовом формате Prometheus."""
    await _refresh_outbox_depth()
    await _refresh_worker_heartbeat_age()
    for name, refresher in _scrape_refreshers.items():
        try:
            await refresher()
        except Exception:  # тот же принцип, что у обновляторов выше: /metrics живёт
            logger.warning("scrape_refresher_failed", refresher=name, exc_info=True)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
