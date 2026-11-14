"""Composition root (Task 0005, Task 0007).

Единственное место, где собирается FastAPI-приложение: подключаются роутеры,
middleware и (в будущем) адаптеры интеграций (P-3, FOUNDATION §5.1). Поэтому
этот модуль намеренно не входит в контракты import-linter, ограничивающие
доменные модули и kernel — ему единственному разрешено видеть все слои.
"""

from __future__ import annotations

from fastapi import FastAPI

from hospitality.ai.gateway.api import refresh_budget_metrics, refresh_call_outcome_metrics
from hospitality.channels.common.retention import refresh_guest_text_age_metrics
from hospitality.channels.showroom.router import router as showroom_router
from hospitality.channels.telegram.router import router as telegram_router
from hospitality.channels.web.documents.router import router as web_documents_router
from hospitality.channels.web.feedback.router import router as web_feedback_router
from hospitality.channels.web.menu.router import router as web_menu_router
from hospitality.channels.web.page import UNAVAILABLE_PAGE as WEB_UNAVAILABLE_PAGE
from hospitality.channels.web.photos.router import router as web_photos_router
from hospitality.channels.web.router import bind_router as web_bind_router
from hospitality.channels.web.router import router as web_router
from hospitality.channels.web.table.router import router as web_table_router
from hospitality.integrations.accounting import build_accounting
from hospitality.integrations.exchange_rates import build_exchange_rates
from hospitality.integrations.file_storage import build_file_storage
from hospitality.integrations.pms import build_pms, build_pms_upload_router
from hospitality.integrations.virus_scan import build_virus_scanner
from hospitality.integrations.weather import build_weather
from hospitality.integrations.web_push import build_web_push
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests.api import router as requests_router
from hospitality.platform.auth import (
    resolve_tenant_from_service_token,
    resolve_tenant_from_staff_session,
)
from hospitality.platform.legal import router as legal_router
from hospitality.preflight import run_preflight
from hospitality.shared.body_limit import BodyLimitMiddleware
from hospitality.shared.config import get_settings
from hospitality.shared.errors import register_error_handlers
from hospitality.shared.health import router as health_router
from hospitality.shared.logging import configure_logging
from hospitality.shared.metrics import register_scrape_refresher
from hospitality.shared.metrics import router as metrics_router
from hospitality.shared.middleware import CorrelationIdMiddleware
from hospitality.shared.ports.accounting import set_accounting
from hospitality.shared.ports.exchange_rates import set_exchange_rates
from hospitality.shared.ports.file_storage import set_file_storage
from hospitality.shared.ports.pms import set_pms
from hospitality.shared.ports.virus_scan import set_virus_scanner
from hospitality.shared.ports.weather import set_weather
from hospitality.shared.ports.web_push import set_web_push
from hospitality.shared.sentry import init_sentry
from hospitality.shared.tenancy import TenantContextMiddleware, chain_resolvers
from hospitality.staff_portal.router import router as staff_portal_router
from hospitality.staff_portal.unclaimed_watch import refresh_forgotten_request_metrics

_DEV_ENVIRONMENT = "dev"


def create_app() -> FastAPI:
    configure_logging(get_settings().log_level)
    # Конфигурация — до всего остального (issue #137): модель вне прайс-листа
    # gateway вскрылась бы только после ответа провайдера, то есть 500-кой у
    # первого же гостя и неучтённым расходом. Здесь она валит старт процесса,
    # то есть деплой — у разработчика, а не диалог у гостя. В контейнере тот же
    # список проверок отрабатывает раньше — в ENTRYPOINT образа (issue #267):
    # под супервизорами uvicorn падение отсюда убивает только ребёнка.
    run_preflight()
    # Sentry — до сборки приложения: интеграции Starlette/FastAPI (Task 0018,
    # §10.4) подхватывают приложение, собранное после init.
    init_sentry(get_settings())
    # Хранилище файлов (spec 0047, ADR-022): адаптер выбирает только composition
    # root (P-3, контракт 2 import-linter); ядро зовёт его через порт.
    set_file_storage(build_file_storage(get_settings()))
    # Антивирус для PDF и DOCX гостя (spec 0075). Воркер файлов не принимает.
    set_virus_scanner(build_virus_scanner(get_settings()))
    # Веб-пуш кабинета (spec 0050): None — пуша на контуре нет, кнопки нет.
    set_web_push(build_web_push(get_settings()))
    # Бухгалтерия (spec 0052): Excel для 1С со страницы «Документы для АВР».
    set_accounting(build_accounting())
    # Источник проживаний (spec 0058 §2.1): `PMS_BACKEND` выбирает адаптер;
    # у адаптера Opera есть свой маршрут приёма выгрузки — монтируется ниже.
    pms = build_pms(get_settings(), max_guests=guests_api.MAX_GUESTS_PER_STAY)
    set_pms(pms)
    # Погода и официальный курс для хода гостя (spec 0084): спрашиваются только при
    # `ENABLE_CONCIERGE_CITY`; адаптер держит кэш и паузу после отказа — один на процесс.
    set_weather(build_weather(get_settings()))
    set_exchange_rates(build_exchange_rates(get_settings()))
    # Описание API (`/docs`, `/redoc`, `/openapi.json`) — только в dev (ADR-006
    # §7 п. 7, #487): снаружи это карта всех маршрутов, включая вход суперадмина.
    docs_enabled = get_settings().sentry_environment == _DEV_ENVIRONMENT
    app = FastAPI(
        title="AI Hospitality Platform",
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
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
    # Предел тела (issue #968): внутри CorrelationIdMiddleware — отказ 413 несёт
    # correlation id и попадает в http_request, как любой ответ.
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    # Гость, открывший чат, меню или ссылку с листка, на ошибке сервера получает страницу
    # «чат не работает — позвоните на ресепшен», а не JSON (#991).
    register_error_handlers(
        app,
        html_pages={
            web_router.prefix + "/": WEB_UNAVAILABLE_PAGE,
            web_bind_router.prefix + "/": WEB_UNAVAILABLE_PAGE,
        },
    )
    app.include_router(health_router)
    # /metrics анонимен — явное решение (§11), как /health: PII и секретов
    # в метриках нет (Task 0018, обоснование — shared/metrics.py).
    app.include_router(metrics_router)
    # Дневной расход LLM по тенантам (issue #103) считает его владелец —
    # ai/gateway: таблица расхода лежит за границей слоя, и kernel её не видит
    # (R-5). Связывает их composition root — тот, кому видны оба слоя.
    register_scrape_refresher("llm_daily_budget", refresh_budget_metrics)
    # Отказы модели по тенантам (issue #374) — тем же путём: журнал вызовов
    # принадлежит ai/gateway, алертер читает итог из /metrics.
    register_scrape_refresher("llm_call_outcomes", refresh_call_outcome_metrics)
    # Возраст снимка Opera по тенантам (spec 0058 §2.6): снимок отдаёт порт PMS,
    # а читает его модуль guests — kernel его не видит (R-5).
    register_scrape_refresher("pms_snapshot_age", guests_api.refresh_pms_snapshot_age_metrics)
    # Возраст старейшего текста гостя по тенантам (issue #410): сторож обещания
    # «тексты — 90 дней»; `messages` — в композиционном слое, kernel их не видит.
    register_scrape_refresher("guest_text_oldest_age", refresh_guest_text_age_metrics)
    # Невзятые дольше срока команды заявки (issue #1050, ERR-OPS-025): сроки служб —
    # в конфиге тенанта, лестница — в кабинете; kernel не видит ни того, ни другого.
    register_scrape_refresher("forgotten_requests", refresh_forgotten_request_metrics)
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
    # Оценка выполненной заявки (spec 0076) — та же гостевая сессия и префикс /g; раньше
    # чата: файл виджета `/g/static/feedback.js` иначе съела бы страница `/g/{slug}/{room}`.
    app.include_router(web_feedback_router)
    # Фото гостя к заявке (spec 0081 §4) — та же сессия и префикс; раньше чата по той же
    # причине: файл `/g/static/photo.js`.
    app.include_router(web_photos_router)
    app.include_router(web_router)
    # «Заказать в номер» (spec 0038): та же гостевая сессия и тот же префикс /g.
    app.include_router(web_menu_router)
    # «Забронировать столик» (spec 0064): та же гостевая сессия и тот же префикс /g.
    app.include_router(web_table_router)
    # QR-ссылка привязки с карточки заселения и листка гостю (spec 0033
    # §6): короткий префикс /w, тот же канал web, тот же путь создания сессии.
    app.include_router(web_bind_router)
    # Реквизиты для АВР (spec 0049): та же гостевая сессия и тот же префикс /g.
    app.include_router(web_documents_router)
    # 3D-витрина для посетителей без проживания (spec 0069, ADR-025): контекст тенанта
    # ставит сама по slug, доступ — код отеля (cookie) или публичный режим; без входа.
    app.include_router(showroom_router)
    # Выгрузка Opera (spec 0058 §2.4): аутентифицируется токеном загрузки
    # тенанта, контекст тенанта ставит сама по slug — как веб-чат. У Fake
    # маршрута нет: снимок двигают тесты, а не отель.
    pms_upload_router = build_pms_upload_router(pms)
    if pms_upload_router is not None:
        app.include_router(pms_upload_router)
    # Кабинет персонала (spec 0033, ADR-014): server-rendered страницы; контекст
    # тенанта ставит звено staff-сессии в цепочке выше, авторизацию действия —
    # сама страница (require_role). Появление входа — решение спеки 0033 (PR C).
    app.include_router(staff_portal_router)
    return app


app = create_app()
