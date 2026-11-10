"""Канон страницы отеля (P-12): авторизация `page_context`, рендер `hotel_page`,
«нет доступа» `forbidden_page` и роли страниц.

Страницы разделов живут в `*_pages.py` — по файлу на раздел `readme/`, подключает
их `router.py`. Новая страница копирует канон: `page_context` с ролью отсюда,
ответ — только `hotel_page`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Final

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_auth import TENANT_SLUG_PATH_PARAM, StaffContext
from hospitality.shared.errors import AppError
from hospitality.staff_portal import browser, shell, signal
from hospitality.staff_portal import queue as queue_page
from hospitality.staff_portal.browser import html_page
from hospitality.staff_portal.push_api import push_context
from hospitality.staff_portal.rendering import render_page

any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)
# Заселение и карточки Stay — служба ресепшена любого уровня и менеджер (spec 0045 §2).
reception_role: Final = staff_auth.require_role(StaffRole.MANAGER, or_reception=True)
# Сводка дня, журнал и статистика — старший (числа и строки своих служб) и менеджер
# (spec 0045 §3, spec 0065 §2.3).
senior_role: Final = staff_auth.require_role(StaffRole.SENIOR, StaffRole.MANAGER)
# Сотрудники — только менеджер (мини-матрица §3.2, развилка Ф-3 spec 0033).
manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


async def page_context(
    request: Request, role_dependency: Callable[[Request], Awaitable[StaffContext]]
) -> StaffContext | Response:
    """Канон авторизации страницы кабинета (P-12; PR E/F копируют).

    Та же проверка, что у JSON-действий (`require_role`), но исходы —
    браузерные: 401 (нет/истекла сессия) → редирект на вход этого отеля
    (slug известен из пути, spec 0037 §3), 403 (нет членства/роли,
    несовпадение RLS-контекста) → HTML «нет доступа» вместо JSON-конверта.
    Сверку RLS-контекста запроса с тенантом страницы выполняет сам
    `require_role` (fail-closed, ревью PR #153) — и страницы, и JSON-действия
    под одной защитой. На 403 сессия заведомо жива (иначе был бы 401) —
    страница показывает «Выйти» и ссылку «войти в этот отель под другим
    логином»: признак `?switch=1` нужен из-за случая «не та роль», где
    членство есть и вход без признака вернул бы 303 в тот же кабинет.
    """
    tenant_slug = str(request.path_params[TENANT_SLUG_PATH_PARAM])
    try:
        return await role_dependency(request)
    except AppError as error:
        if error.status_code == 401:
            return RedirectResponse(browser.hotel_login_path(tenant_slug), status_code=303)
        return forbidden_page(tenant_slug)


def forbidden_page(tenant_slug: str) -> Response:
    """HTML «нет доступа» с выходом и входом под другим логином (канон `page_context`)."""
    return html_page(
        render_page(
            "forbidden.html",
            show_logout=True,
            switch_url=browser.hotel_login_path(tenant_slug, switch=True),
        ),
        status_code=403,
    )


async def hotel_page(
    staff: StaffContext, template: str, *, status_code: int = 200, **context: Any
) -> Response:
    """Страница отеля: каркас с сигналом кабинета (spec 0039 §3.1).

    Сигнал обязан звучать на КАЖДОЙ странице отеля — ЧП может прийти, пока
    ресепшен заселяет гостя, — поэтому страницы отеля рендерятся только
    отсюда: новая страница, отданная мимо, молча осталась бы без сигнала.
    Страницы входа, приглашения и «нет доступа» сюда не ходят: опрашивать
    сигнал там нечем (нет сессии или членства). Каркас с боковым меню (#462) —
    тоже только отсюда: меню строится по роли вошедшего и по конфигу отеля
    («Товары и меню» — только при открытом каталоге room service, spec 0038 Р-10).
    """
    config = await queue_page.tenant_config(staff)
    # Каркас v3 (#635): число открытых заявок у пункта меню и «Сменить отель»
    # при нескольких членствах — на каждой странице отеля, поэтому здесь.
    memberships = await staff_auth.list_memberships(staff.user_id)
    return html_page(
        render_page(
            template,
            signal=signal.signal_context(staff.tenant_slug),
            # Уведомления на телефон (spec 0050): None — пуша на контуре нет.
            push=push_context(staff.tenant_slug),
            shell=shell.shell_context(
                staff,
                template,
                config,
                open_requests=await shell.open_requests_count(staff, config),
                several_hotels=len(memberships) > 1,
            ),
            **context,
        ),
        status_code=status_code,
    )
