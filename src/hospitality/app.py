"""Composition root (Task 0005, Task 0007).

Единственное место, где собирается FastAPI-приложение: подключаются роутеры,
middleware и (в будущем) адаптеры интеграций (P-3, FOUNDATION §5.1). Поэтому
этот модуль намеренно не входит в контракты import-linter, ограничивающие
доменные модули и kernel — ему единственному разрешено видеть все слои.
"""

from __future__ import annotations

from fastapi import FastAPI

from hospitality.shared.config import get_settings
from hospitality.shared.errors import register_error_handlers
from hospitality.shared.health import router as health_router
from hospitality.shared.logging import configure_logging
from hospitality.shared.middleware import CorrelationIdMiddleware


def create_app() -> FastAPI:
    configure_logging(get_settings().log_level)
    app = FastAPI(title="AI Hospitality Platform")
    app.add_middleware(CorrelationIdMiddleware)
    register_error_handlers(app)
    app.include_router(health_router)
    return app


app = create_app()
