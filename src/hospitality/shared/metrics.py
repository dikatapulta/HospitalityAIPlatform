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
- Глубина outbox считается в момент scrape запросом к БД через
  ``platform_session_scope`` (outbox — кросс-тенантная таблица, как в
  ``deliver_pending_events``). Недоступная БД не роняет ``/metrics``
  (алертер обязан продолжать читать счётчики 5xx): gauge = NaN + WARNING.

``/metrics`` анонимен — явное решение (§11), симметрично ``/health/*``:
PII и секретов в метриках нет; токен для алертера — лишняя связность Phase 0.
Пересмотр при выходе в прод — «Известные отступления» README ``shared``.
"""

from __future__ import annotations

import math
from decimal import Decimal

from fastapi import APIRouter
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select

from hospitality.shared.db import platform_session_scope
from hospitality.shared.events import OutboxEvent
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
outbox_pending_events = Gauge(
    "outbox_pending_events",
    "Недоставленные события outbox (processed_at IS NULL); NaN — БД недоступна",
)

router = APIRouter(tags=["metrics"])


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


async def _refresh_outbox_depth() -> None:
    try:
        async with platform_session_scope() as session:
            pending = await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.processed_at.is_(None))
            )
        outbox_pending_events.set(pending if pending is not None else 0)
    except Exception:  # диагностический путь: метрики живут, когда БД мертва
        logger.warning("outbox_depth_unavailable", exc_info=True)
        outbox_pending_events.set(math.nan)


@router.get("/metrics")
async def metrics_endpoint() -> Response:
    """Выдача всех метрик процесса в текстовом формате Prometheus."""
    await _refresh_outbox_depth()
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
