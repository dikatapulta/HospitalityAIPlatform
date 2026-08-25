"""Данные страницы «Новая заявка» (spec 0035 §5, PR C серии #299).

Форма ручного приёма: заявку, о которой гость сказал по телефону или поймал
горничную в коридоре, до этой страницы вносить было некуда — `create_request`
звали только AI-инструмент и публичная дверь. Без неё у Exit-критерия Phase 1
«≥ 70% заявок без участия ресепшена» нет знаменателя.

Три поля (§5): номер комнаты — единственный ввод с клавиатуры, служба —
чипсы по категориям тенанта, текст → `summary`. Само создание — JSON-действие
`api_router.py` через `requests_api.create_request` (P-5); здесь только чтение
категорий и контекст шаблона.
"""

from __future__ import annotations

from typing import Any

from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_auth import StaffContext

# Потолок сути заявки — граница схемы `ServiceRequestCreate` (P-12: одно число,
# не второй экземпляр). Поле `maxlength` формы обязано совпадать с ней, иначе
# сотрудник допишет фразу и получит отказ уже после нажатия кнопки.
SUMMARY_MAX_LENGTH = 500
ROOM_NUMBER_MAX_LENGTH = 20


async def build_new_request_context(staff: StaffContext) -> dict[str, Any]:
    """Контекст шаблона `new_request.html`: службы отеля чипсами + адрес действия.

    Вызывается внутри контекста тенанта запроса (его ставит звено
    `TenantResolver` кабинета) — категории читаются под RLS текущего тенанта,
    поэтому чужих служб в чипсах не окажется.

    Отель без единой категории (онбординг не завершён, служебный smoke-тенант)
    страницу не роняет: шаблон показывает подсказку вместо формы — та же
    деградация, что у очереди без конфига тенанта.
    """
    categories = await requests_api.list_categories()
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "categories": [{"id": str(category.id), "name": category.name} for category in categories],
        "summary_max_length": SUMMARY_MAX_LENGTH,
        "room_max_length": ROOM_NUMBER_MAX_LENGTH,
        "create_endpoint": f"/staff/{staff.tenant_slug}/api/requests",
        "queue_path": f"/staff/{staff.tenant_slug}/requests",
    }
