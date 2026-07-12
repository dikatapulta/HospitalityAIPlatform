"""Composition root (Task 0005, Task 0007).

Единственное место, где собирается FastAPI-приложение: подключаются роутеры,
middleware и (в будущем) адаптеры интеграций (P-3, FOUNDATION §5.1). Поэтому
этот модуль намеренно не входит в контракты import-linter, ограничивающие
доменные модули и kernel — ему единственному разрешено видеть все слои.
"""

from __future__ import annotations

from fastapi import FastAPI

from hospitality.modules.requests.api import router as requests_router
from hospitality.platform.auth import resolve_tenant_from_service_token
from hospitality.shared.config import get_settings
from hospitality.shared.errors import register_error_handlers
from hospitality.shared.health import router as health_router
from hospitality.shared.logging import configure_logging
from hospitality.shared.middleware import CorrelationIdMiddleware
from hospitality.shared.tenancy import TenantContextMiddleware


def create_app() -> FastAPI:
    configure_logging(get_settings().log_level)
    app = FastAPI(title="AI Hospitality Platform")
    # Порядок фиксирован (последний добавленный — внешний): CorrelationIdMiddleware
    # обязан быть снаружи — он очищает контекст логирования в начале запроса и
    # пишет http_request в конце; TenantContextMiddleware внутри него биндит
    # tenant_id, находя тенанта по сервисному токену (Task 0013, §11).
    app.add_middleware(TenantContextMiddleware, resolver=resolve_tenant_from_service_token)
    app.add_middleware(CorrelationIdMiddleware)
    register_error_handlers(app)
    app.include_router(health_router)
    app.include_router(requests_router)
    return app


app = create_app()
