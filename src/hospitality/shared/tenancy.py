"""Канонический контекст тенанта (Task 0009, P-4, ADR-003, FOUNDATION §6).

Единственный способ выполнить код «от имени тенанта»:

    from hospitality.shared.tenancy import tenant_context

    with tenant_context(tenant_id):
        async with session_scope() as session:
            ...

`tenant_context` устанавливает contextvar текущего тенанта и добавляет
`tenant_id` в контекст логирования; `session_scope()` (shared/db.py) читает
contextvar и ставит `SET LOCAL`-настройку в транзакции — RLS-политики
Postgres видят её через `current_setting('app.tenant_id', true)`.

Кто входит в контекст тенанта:
- HTTP-запросы — `TenantContextMiddleware` (резолвер тенанта появится
  в Task 0013 вместе с аутентификацией; до тех пор HTTP-запросы работают
  без тенанта — тенантных эндпоинтов ещё нет);
- каналы (Telegram, Task 0016) — `tenant_context()` в обработчике вебхука
  по маппингу чата из конфига;
- воркер (Task 0010) — `tenant_context()` на каждое событие по его tenant_id;
- тесты и сиды — `tenant_context()` напрямую.

Примечание к карте фазы: карточка 0009 называет файл `platform/tenancy.py`,
но канон обязан жить в `shared` — его читает `shared/db.py`, а направление
слоёв `platform → shared` (R-5, контракт import-linter) запрещает обратный
импорт.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog
from starlette.types import ASGIApp, Receive, Scope, Send

# Имя GUC-переменной Postgres, через которую RLS-политики видят текущего
# тенанта. Контракт трёх мест: session_scope() (ставит), RLS-политики миграций
# (читают), тест изоляции (проверяет). Менять синхронно со всеми политиками.
TENANT_ID_GUC = "app.tenant_id"

# Резолвер тенанта для HTTP: по ASGI-scope (заголовки, путь, авторизация)
# возвращает tenant_id или None. Реальная реализация — по сервисному токену
# (Task 0013); принимать tenant_id из произвольных клиентских заголовков
# запрещено (§11: клиент не выбирает себе тенанта).
TenantResolver = Callable[[Scope], uuid.UUID | None]

_current_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("current_tenant_id", default=None)


class TenantContextRequiredError(RuntimeError):
    """Код, работающий с тенантными данными, вызван вне `tenant_context`.

    Это ошибка программирования (нарушение P-4), а не ожидаемая бизнес-ошибка,
    поэтому не AppError: наружу уходит канонический 500 (ERR-PLATFORM-001).
    """

    def __init__(self) -> None:
        super().__init__(
            "tenant context is not set: wrap the call in tenant_context(tenant_id) "
            "(P-4, ADR-003); for platform-level work use platform_session_scope()"
        )


def current_tenant_id() -> uuid.UUID:
    """Текущий тенант; вне контекста тенанта — исключение (P-4)."""
    tenant_id = _current_tenant_id.get()
    if tenant_id is None:
        raise TenantContextRequiredError()
    return tenant_id


def current_tenant_id_or_none() -> uuid.UUID | None:
    """Текущий тенант или None — только для диагностики и логирования.

    В бизнес-коде используйте `current_tenant_id()`: молчаливое «без тенанта»
    там — дыра в изоляции.
    """
    return _current_tenant_id.get()


@contextmanager
def tenant_context(tenant_id: uuid.UUID) -> Iterator[None]:
    """Выполнить блок кода от имени тенанта (канон, P-12).

    Устанавливает contextvar тенанта и поле `tenant_id` в контексте
    логирования; на выходе восстанавливает прежние значения (вложенные
    контексты допустимы: воркер обрабатывает события разных тенантов
    в одном процессе).
    """
    token = _current_tenant_id.set(tenant_id)
    log_tokens = structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))
    try:
        yield
    finally:
        _current_tenant_id.reset(token)
        structlog.contextvars.reset_contextvars(**log_tokens)


class TenantContextMiddleware:
    """Входит в контекст тенанта на время HTTP-запроса.

    Без резолвера — прозрачный проходной слой: тенантных эндпоинтов в Phase 0
    ещё нет, а точка подключения и порядок относительно CorrelationIdMiddleware
    (см. composition root) зафиксированы заранее.

    Контекст логирования намеренно НЕ очищается на выходе: событие
    `http_request` пишется во внешнем CorrelationIdMiddleware уже после этого
    слоя и должно содержать tenant_id; контекст каждого запроса начинается
    с clear_contextvars() там же.
    """

    def __init__(self, app: ASGIApp, resolver: TenantResolver | None = None) -> None:
        self._app = app
        self._resolver = resolver

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._resolver is None:
            await self._app(scope, receive, send)
            return

        tenant_id = self._resolver(scope)
        if tenant_id is None:
            await self._app(scope, receive, send)
            return

        token = _current_tenant_id.set(tenant_id)
        structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))
        try:
            await self._app(scope, receive, send)
        finally:
            _current_tenant_id.reset(token)
