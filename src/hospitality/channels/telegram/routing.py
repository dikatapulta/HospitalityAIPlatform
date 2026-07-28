"""Адресат staff-уведомления: чат службы по категории заявки (spec 0026).

Одно правило — одно место (P-12). Им пользуются все, кто пишет персоналу:
подписчики событий (`notifications.py`) и напоминания о невзятых заявках
(`reminders.py`, spec 0028). Раньше эти функции жили приватными внутри
`notifications.py`; с появлением второго потребителя они вынесены сюда без
изменения поведения — копия правила в новом файле означала бы два места, где
решается, куда уходит сообщение службе.

Правило: чат категории заявки (`TenantConfig.staff_chats_by_category`) →
дефолтный чат инсталляции (`TELEGRAM_STAFF_CHAT_ID`). Недоступный конфиг или
незнакомая категория — дефолтный чат и WARNING: уведомление важнее
маршрутизации (§7.8), служба прочитает его в общей ленте, а не потеряет.
"""

from __future__ import annotations

import uuid

import structlog

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import load_tenant_config
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): сообщение персоналу не
# доставлено, потому что не настроен ни чат категории, ни дефолтный. Лог-код
# (не AppError): у подписчика/фоновой задачи нет клиента, которому отвечать
# статусом.
ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED = "ERR-TELEGRAM-002"


async def resolve_category(category_id: uuid.UUID) -> requests_api.RequestCategoryRead | None:
    """Категория заявки по id; None — такой категории у тенанта нет.

    Одно чтение на уведомление: из категории берутся и человекочитаемое имя для
    текста, и `key` для выбора чата службы (spec 0026).
    """
    for category in await requests_api.list_categories():
        if category.id == category_id:
            return category
    return None


def category_name(category: requests_api.RequestCategoryRead | None, category_id: uuid.UUID) -> str:
    """Человекочитаемое имя категории для уведомления; id как фолбэк."""
    return category.name if category is not None else str(category_id)


async def staff_chat_for_category(
    category: requests_api.RequestCategoryRead | None, default_staff_chat_id: str
) -> str:
    """Чат службы по категории заявки; фолбэк — дефолтный чат (spec 0026).

    Деградация та же, что у языка гостя (`_tenant_default_language`): конфиг
    недоступен (онбординг не завершён, дрейф схемы) → дефолтный чат и WARNING.
    """
    if category is None:
        return default_staff_chat_id
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        logger.warning(
            "staff_routing_config_unavailable",
            error_code=error.code,
            category_key=category.key,
        )
        return default_staff_chat_id
    return config.staff_chat_for(category.key, default=default_staff_chat_id)


def log_routing(
    staff_chat_id: str,
    category: requests_api.RequestCategoryRead | None,
    default_staff_chat_id: str,
) -> None:
    """След маршрутизации (spec 0026): дошло ли сообщение до СВОЕЙ службы."""
    logger.info(
        "staff_notification_routed",
        category_key=category.key if category is not None else None,
        chat_id=staff_chat_id,
        routed=staff_chat_id != default_staff_chat_id,
    )


def current_correlation_id() -> str:
    """correlation_id текущего следа: у подписчика — событие (его восстановил
    доставщик outbox), у фоновой задачи — id прогона (§10.2)."""
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else ""
