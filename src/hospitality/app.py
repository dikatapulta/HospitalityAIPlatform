"""Composition root (Task 0005, Task 0007).

Единственное место, где собирается FastAPI-приложение: подключаются роутеры,
middleware и (в будущем) адаптеры интеграций (P-3, FOUNDATION §5.1). Поэтому
этот модуль намеренно не входит в контракты import-linter, ограничивающие
доменные модули и kernel — ему единственному разрешено видеть все слои.
"""

from __future__ import annotations

from fastapi import FastAPI

from hospitality.channels.telegram.router import router as telegram_router
from hospitality.modules.requests.api import router as requests_router
from hospitality.platform.auth import resolve_tenant_from_service_token
from hospitality.shared.config import get_settings
from hospitality.shared.errors import register_error_handlers
from hospitality.shared.health import router as health_router
from hospitality.shared.logging import configure_logging
from hospitality.shared.metrics import router as metrics_router
from hospitality.shared.middleware import CorrelationIdMiddleware
from hospitality.shared.sentry import init_sentry
from hospitality.shared.tenancy import TenantContextMiddleware


def create_app() -> FastAPI:
    configure_logging(get_settings().log_level)
    # Sentry — до сборки приложения: интеграции Starlette/FastAPI (Task 0018,
    # §10.4) подхватывают приложение, собранное после init.
    init_sentry(get_settings())
    app = FastAPI(title="AI Hospitality Platform")
    # Порядок фиксирован (последний добавленный — внешний): CorrelationIdMiddleware
    # обязан быть снаружи — он очищает контекст логирования в начале запроса и
    # пишет http_request в конце; TenantContextMiddleware внутри него биндит
    # tenant_id, находя тенанта по сервисному токену (Task 0013, §11).
    app.add_middleware(TenantContextMiddleware, resolver=resolve_tenant_from_service_token)
    app.add_middleware(CorrelationIdMiddleware)
    register_error_handlers(app)
    app.include_router(health_router)
    # /metrics анонимен — явное решение (§11), как /health: PII и секретов
    # в метриках нет (Task 0018, обоснование — shared/metrics.py).
    app.include_router(metrics_router)
    app.include_router(requests_router)
    # Вебхук Telegram (Task 0016): аутентифицируется секретом вебхука, не сервисным
    # токеном, — поэтому вне зависимости require_authenticated_tenant роутера /api/v1.
    app.include_router(telegram_router)
    return app


app = create_app()
