"""Composition root (Task 0005, Task 0007).

Единственное место, где собирается FastAPI-приложение: подключаются роутеры,
middleware и (в будущем) адаптеры интеграций (P-3, FOUNDATION §5.1). Поэтому
этот модуль намеренно не входит в контракты import-linter, ограничивающие
доменные модули и kernel — ему единственному разрешено видеть все слои.
"""

from __future__ import annotations

from fastapi import FastAPI

from hospitality.ai.gateway.api import refresh_budget_metrics
from hospitality.channels.telegram.router import router as telegram_router
from hospitality.channels.web.router import bind_router as web_bind_router
from hospitality.channels.web.router import router as web_router
from hospitality.modules.requests.api import router as requests_router
from hospitality.platform.auth import (
    resolve_tenant_from_service_token,
    resolve_tenant_from_staff_session,
)
from hospitality.platform.legal import router as legal_router
from hospitality.shared.config import get_settings
from hospitality.shared.errors import register_error_handlers
from hospitality.shared.health import router as health_router
from hospitality.shared.logging import configure_logging
from hospitality.shared.metrics import register_scrape_refresher
from hospitality.shared.metrics import router as metrics_router
from hospitality.shared.middleware import CorrelationIdMiddleware
from hospitality.shared.sentry import init_sentry
from hospitality.shared.tenancy import TenantContextMiddleware, chain_resolvers
from hospitality.staff_portal.router import router as staff_portal_router


def create_app() -> FastAPI:
    configure_logging(get_settings().log_level)
    # Sentry — до сборки приложения: интеграции Starlette/FastAPI (Task 0018,
    # §10.4) подхватывают приложение, собранное после init.
    init_sentry(get_settings())
    app = FastAPI(title="AI Hospitality Platform")
    # Порядок фиксирован (последний добавленный — внешний): CorrelationIdMiddleware
    # обязан быть снаружи — он очищает контекст логирования в начале запроса и
    # пишет http_request в конце; TenantContextMiddleware внутри него биндит
    # tenant_id по цепочке идентичностей ADR-008 §6: сервисный токен (Task 0013,
    # §11) → staff-сессия + slug кабинета (spec 0033, PR C). Гостевая сессия в
    # цепочку не входит — гостевой канал ставит контекст сам (см. web_router ниже).
    app.add_middleware(
        TenantContextMiddleware,
        resolver=chain_resolvers(
            resolve_tenant_from_service_token, resolve_tenant_from_staff_session
        ),
    )
    app.add_middleware(CorrelationIdMiddleware)
    register_error_handlers(app)
    app.include_router(health_router)
    # /metrics анонимен — явное решение (§11), как /health: PII и секретов
    # в метриках нет (Task 0018, обоснование — shared/metrics.py).
    app.include_router(metrics_router)
    # Дневной расход LLM по тенантам (issue #103) считает его владелец —
    # ai/gateway: таблица расхода лежит за границей слоя, и kernel её не видит
    # (R-5). Связывает их composition root — тот, кому видны оба слоя.
    register_scrape_refresher("llm_daily_budget", refresh_budget_metrics)
    # Политика конфиденциальности (spec 0029 §2): публичная страница вне контекста
    # тенанта, как /health и /metrics, — на неё ссылается текст согласия гостя.
    app.include_router(legal_router)
    app.include_router(requests_router)
    # Вебхук Telegram (Task 0016): аутентифицируется секретом вебхука, не сервисным
    # токеном, — поэтому вне зависимости require_authenticated_tenant роутера /api/v1.
    app.include_router(telegram_router)
    # Веб-чат гостя (spec 0027): контекст тенанта ставит сам по QR-slug внутри
    # своих маршрутов; аутентификация — GuestSession по коду заселения (ADR-008),
    # строгий auth-only. В общий TenantResolver гостевые сессии не входят.
    app.include_router(web_router)
    # Одноразовая QR-ссылка привязки со страницы заселения кабинета (spec 0033
    # §6): короткий префикс /w, тот же канал web, тот же путь создания сессии.
    app.include_router(web_bind_router)
    # Кабинет персонала (spec 0033, ADR-014): server-rendered страницы; контекст
    # тенанта ставит звено staff-сессии в цепочке выше, авторизацию действия —
    # сама страница (require_role). Появление входа — решение спеки 0033 (PR C).
    app.include_router(staff_portal_router)
    return app


app = create_app()
