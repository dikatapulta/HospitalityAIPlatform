"""Composition root (Task 0005).

Единственное место, где собирается FastAPI-приложение: подключаются роутеры,
middleware и (в будущем) адаптеры интеграций (P-3, FOUNDATION §5.1). Поэтому
этот модуль намеренно не входит в контракты import-linter, ограничивающие
доменные модули и kernel — ему единственному разрешено видеть все слои.
"""

from __future__ import annotations

from fastapi import FastAPI

from hospitality.shared.health import router as health_router


def create_app() -> FastAPI:
    app = FastAPI(title="AI Hospitality Platform")
    app.include_router(health_router)
    return app


app = create_app()
