"""Страницы раздела управления (`readme/management.md`): сводка дня, справочник
отеля, вид гостевого чата, журнал изменений, статистика и «Сотрудники».
Главная отеля — в `router.py`.

Канон авторизации и рендера — `pages.py`; действия — `knowledge_api.py`,
`chat_look_api.py`, `team_api.py` и `team_invites_api.py`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from hospitality.staff_portal import changes, chat_look, knowledge, pages, stats, summary, team
from hospitality.staff_portal import queue as queue_page

router = APIRouter(tags=["staff-portal"])


@router.get(
    "/{tenant_slug}/summary",
    response_class=HTMLResponse,
    summary="Сводка дня: числа за сутки отеля (spec 0035 §7, закрывает #300)",
)
async def summary_page(request: Request, tenant_slug: str) -> Response:
    """Один день отеля числами: создано/закрыто, откуда пришли, медиана взятия,
    просрочено за день, эскалации, открыто сейчас; ниже — разрез по службам.
    Уровень `senior` (числа своих служб, без эскалаций) или `manager`
    (docs/RBAC.md). Своих действий у страницы нет — только чтение, поэтому ни
    JSON-эндпоинта, ни поллинга."""
    del tenant_slug
    result = await pages.page_context(request, pages.senior_role)
    if isinstance(result, Response):
        return result
    context = await summary.build_summary_context(
        result, day=summary.parse_summary_day(request.query_params.get("day"))
    )
    return await pages.hotel_page(result, "summary.html", **context)


@router.get(
    "/{tenant_slug}/knowledge",
    response_class=HTMLResponse,
    summary="Справочник отеля: что бот отвечает гостям (spec 0036 §7, #335)",
)
async def knowledge_page(request: Request, tenant_slug: str) -> Response:
    """Факты отеля в порядке хранения, счётчик бюджета, форма добавления и
    вопросы гостей без ответа за 7 дней (spec 0036 §7 п. 3). Роль `manager`
    (docs/RBAC.md); действия — JSON-эндпоинты `knowledge_api.py` под той же
    ролью, после действия `static/knowledge.js` перезагружает страницу."""
    del tenant_slug
    result = await pages.page_context(request, pages.manager_role)
    if isinstance(result, Response):
        return result
    context = await knowledge.build_knowledge_context(result)
    return await pages.hotel_page(result, "knowledge.html", **context)


@router.get(
    "/{tenant_slug}/chat-look",
    response_class=HTMLResponse,
    summary="Вид гостевого чата: цвет подложки, узор, знак отеля (spec 0066, #761)",
)
async def chat_look_page(request: Request, tenant_slug: str) -> Response:
    """Варианты, узор, знак и два телефона предпросмотра — день и ночь. Роль
    `manager` (docs/RBAC.md); действия и предпросмотр — `chat_look_api.py`."""
    del tenant_slug
    result = await pages.page_context(request, pages.manager_role)
    if isinstance(result, Response):
        return result
    config = await queue_page.tenant_config(result)
    return await pages.hotel_page(
        result, "chat_look.html", **chat_look.build_context(result, config)
    )


@router.get(
    "/{tenant_slug}/changes",
    response_class=HTMLResponse,
    summary="Журнал изменений: кто что сделал (spec 0041, закрывает #370)",
)
async def changes_page(request: Request, tenant_slug: str) -> Response:
    """Действия сотрудников с заявками и заселением за день отеля; фильтры
    «день / сотрудник / действие» — GET-форма. Уровень `senior` (строки своих
    служб) или `manager` (docs/RBAC.md). Только чтение — ни JSON-эндпоинта, ни
    поллинга (канон сводки дня)."""
    del tenant_slug
    result = await pages.page_context(request, pages.senior_role)
    if isinstance(result, Response):
        return result
    context = await changes.build_changes_context(result, dict(request.query_params))
    return await pages.hotel_page(result, "changes.html", **context)


@router.get(
    "/{tenant_slug}/stats",
    response_class=HTMLResponse,
    summary="Статистика: сколько заявок взял и выполнил каждый сотрудник (spec 0065, #704)",
)
async def stats_page(request: Request, tenant_slug: str) -> Response:
    """Итоги по сотруднику за «Сегодня» · «7 дней» · «30 дней» — ссылками.
    Уровень `senior` (итоги своих служб) или `manager` (docs/RBAC.md). Только
    чтение — ни JSON-эндпоинта, ни поллинга (канон сводки дня). Раздел выключен
    (`ENABLE_STAFF_STATS`) — 404, как у неизвестного адреса."""
    del tenant_slug
    if not stats.section_enabled():
        raise HTTPException(status_code=404)
    result = await pages.page_context(request, pages.senior_role)
    if isinstance(result, Response):
        return result
    context = await stats.build_stats_context(
        result, period=stats.parse_stats_period(request.query_params.get("period"))
    )
    return await pages.hotel_page(result, "stats.html", **context)


@router.get(
    "/{tenant_slug}/team",
    response_class=HTMLResponse,
    summary="Сотрудники: состав, приглашения, роли (spec 0033 §7)",
)
async def team_page(request: Request, tenant_slug: str) -> Response:
    """Список членств отеля + форма приглашения + ожидающие ссылки.
    Роль `manager` (мини-матрица §3.2); действия — JSON-эндпоинты
    `team_api.py`, ссылка приглашения показывается один раз из `team.js`."""
    del tenant_slug
    result = await pages.page_context(request, pages.manager_role)
    if isinstance(result, Response):
        return result
    return await pages.hotel_page(result, "team.html", **await team.build_team_context(result))
