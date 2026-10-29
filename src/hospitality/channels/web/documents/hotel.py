"""Где работает форма реквизитов и в каком поясе отель (spec 0049 Р-7, §6).

Отдельный модуль — чтобы и страница чата (`channels/web/router.py`, скрепка), и
маршруты формы спрашивали одно и то же без циклического импорта. Обе функции
зовутся внутри `tenant_context`.
"""

from __future__ import annotations

from datetime import UTC, tzinfo

from hospitality.modules.documents import api as documents_api
from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import current_tenant_id


async def guest_documents_enabled() -> bool:
    """Скрепка и маршруты формы: выключатель контура И служба «Бухгалтерия» отеля.

    Выключатель `ENABLE_GUEST_DOCUMENTS` — свойство контура и его хостинга
    (где лежат документы гостя, ADR-006), служба — данные отеля (P-11, NG-7).
    """
    if not get_settings().enable_guest_documents:
        return False
    return await documents_api.accepts_act_requests()


async def hotel_zone() -> tzinfo:
    """Пояс отеля для дат гостю; тенант без конфига — UTC (дата хотя бы не выдумана)."""
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        return UTC
    return config.tzinfo
