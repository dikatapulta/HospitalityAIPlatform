"""Данные страницы «Новая заявка» (spec 0035 §5, PR C серии #299).

Форма ручного приёма: заявку, о которой гость сказал по телефону или поймал
горничную в коридоре, до этой страницы вносить было некуда — `create_request`
звали только AI-инструмент и публичная дверь. Без неё у Exit-критерия Phase 1
«≥ 70% заявок без участия ресепшена» нет знаменателя.

Три поля (§5): номер комнаты — единственный ввод с клавиатуры, служба —
чипсы по категориям тенанта, текст → `summary`. Само создание — JSON-действие
`queue_api.py` через `requests_api.create_request` (P-5); здесь только чтение
категорий и контекст шаблона.

Кабинет v3 (#636): та же форма — окном поверх любой страницы отеля
(`_app_windows.html` в `layout.html`), страница остаётся для прямых ссылок.
Поля окна приходят фрагментом при открытии (`fields` в requests_pages.py), а не лежат
в каждой странице: службы отеля читаются только тогда, когда форма нужна, и
названия чужих служб не попадают в сводку и журнал старшего (spec 0045 §3).
Флажок «Сразу взять себе» — второе действие того же JS, существующий
`POST …/requests/{id}/claim`; чипс службы, которой у сотрудника нет, помечен,
чтобы флажок гас заранее, а не отказом после создания.
"""

from __future__ import annotations

from typing import Any

from hospitality.modules.requests import api as requests_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.staff_portal import scope

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
    return {
        "display_name": staff.display_name,
        "tenant_name": staff.tenant_name,
        "tenant_slug": staff.tenant_slug,
        "new_request": await build_form_context(staff),
    }


def form_paths(tenant_slug: str) -> dict[str, str]:
    """Адреса формы: создание, «взять», очередь и поля окна — одним местом
    для страницы и окна каркаса (`shell.shell_context`)."""
    queue_path = f"/staff/{tenant_slug}/requests"
    return {
        "create_endpoint": f"/staff/{tenant_slug}/api/requests",
        "actions_endpoint": f"/staff/{tenant_slug}/api/requests",
        "queue_path": queue_path,
        "fields_path": f"{queue_path}/new/fields",
    }


async def build_form_context(staff: StaffContext) -> dict[str, Any]:
    """Поля формы `_new_request_form.html` — одной на страницу и окно (#636).

    `own` у службы — сотрудник может взять её заявку (spec 0045 §3): у ресепшена
    и менеджера — все службы. Право всё равно проверяет `claim` на сервере;
    пометка лишь гасит флажок «Сразу взять себе» до отправки.
    """
    categories = await requests_api.list_categories()
    allowed = scope.allowed_category_ids(staff, categories)
    return {
        "categories": [
            {
                "id": str(category.id),
                "name": category.name,
                "own": allowed is None or category.id in allowed,
            }
            for category in categories
        ],
        "summary_max_length": SUMMARY_MAX_LENGTH,
        "room_max_length": ROOM_NUMBER_MAX_LENGTH,
        **form_paths(staff.tenant_slug),
    }
