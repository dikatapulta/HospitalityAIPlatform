"""Где работает форма реквизитов, в каком поясе отель и читает ли фото модель (spec 0049).

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


def photos_read_by_ai() -> bool:
    """Фото формы уходит модели — то же условие, что у воркера (`ai/act_requisites.py`, Р-13).

    Настройку читают оба процесса: воркер — для разбора, `app` — для подписи формы,
    поэтому compose передаёт `LLM_MODEL_DOCUMENTS` обоим из одного `.env`.
    """
    return bool(get_settings().llm_model_documents)


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
