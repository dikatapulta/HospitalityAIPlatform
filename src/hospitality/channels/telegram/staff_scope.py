"""Граница службы для Telegram-чатов персонала (spec 0074 §4, spec 0045 §5).

Кабинет знает службы сотрудника по членству; у Telegram-чата личности нет, поэтому
службы чата выводятся обратным правилом из маршрутизации уведомлений
(`TenantConfig.staff_chats_by_category`, spec 0026): чат службы ведёт заявки
категорий, размеченных на него. Все службы — как ресепшен и менеджер в RBAC — у
дефолтного чата инсталляции (туда же уходит всё неразмеченное), у чата утренней
сводки (группа руководства) и у чата службы ресепшена.

Домен проверяет службу сам, под блокировкой строки (`change_request_status(...,
allowed_category_ids=…)`, ERR-REQUESTS-007); здесь только перевод «чат → id
категорий» и тот же признак для поиска `#N` и кнопок до перехода.
"""

from __future__ import annotations

import uuid

from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import load_tenant_config
from hospitality.platform.staff_services import RECEPTION_SERVICE_KEY
from hospitality.shared.config import get_settings
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Ответ персоналу на заявку чужой службы — и командой, и тостом кнопки.
FOREIGN_SERVICE_HINT = "Это заявка другой службы — её ведёт своя служба или ресепшен."


async def chat_category_ids(chat_id: str) -> frozenset[uuid.UUID] | None:
    """Id категорий, над заявками которых чат вправе работать; None — любые.

    Зовётся внутри `tenant_context`. Конфиг не прочитался — fail-closed: дефолтный
    чат по-прежнему может всё (он персонал и без конфига, `service._staff_chat_ids`),
    чат службы — ничего: открыть ему чужие службы хуже, чем отказать.
    """
    default = get_settings().telegram_staff_chat_id
    if default and chat_id == default:
        return None
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        logger.warning("staff_chat_scope_unavailable", error_code=error.code)
        return frozenset()
    if chat_id == config.daily_summary_chat_id:
        return None
    keys = {key for key, chat in config.staff_chats_by_category.items() if chat == chat_id}
    if RECEPTION_SERVICE_KEY in keys:
        return None
    categories = await requests_api.list_categories()
    return frozenset(category.id for category in categories if category.key in keys)


def is_foreign(
    request: requests_api.ServiceRequestRead, allowed: frozenset[uuid.UUID] | None
) -> bool:
    """Заявка не из служб чата (тот же признак, что `_ensure_own_service` домена)."""
    return allowed is not None and request.category_id not in allowed
