"""Страница ссылки нового пароля `GET/POST /staff/reset/{token}` (spec 0044 §4).

Вторая анонимная дверь по токену — по образцу приглашения (`invites.py`): у
держателя ссылки нет ни сессии, ни контекста тенанта (`reset` — служебный
сегмент `_STAFF_RESERVED_SEGMENTS`), аутентификация — сам одноразовый токен.
Токен маскируется в access-логе и Sentry (`shared/logging._SECRET_PATH_PATTERNS`).

После сохранения пароля — сразу вход каноническим `staff_auth.login` (P-12,
довод `invites.invite_submit`) и главная кабинета; не вышло — экран «пароль
сохранён, войдите сами»: ссылка уже погашена, откатывать нечего. CSRF —
тот же щит формы, что у входа и приглашения (`browser.is_cross_origin`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.platform import staff_auth, staff_password_reset
from hospitality.shared.clientip import client_ip
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import browser
from hospitality.staff_portal.rendering import render_page

logger = get_logger(module=__name__)

router = APIRouter(prefix="/reset", tags=["staff-portal"])


def _invalid_page() -> Response:
    """Неизвестная, истёкшая, использованная, заменённая ссылка — один экран
    (ERR-AUTH-014, причины держателю неразличимы)."""
    return browser.html_page(render_page("reset_invalid.html"), status_code=410)


def _form_page(
    invitation: staff_password_reset.PasswordResetInvitation,
    token: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return browser.html_page(
        render_page(
            "reset.html",
            display_name=invitation.display_name,
            staff_login=invitation.login,
            tenant_name=invitation.tenant_name,
            tenant_slug=invitation.tenant_slug,
            action=f"/staff/reset/{token}",
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/{token}", response_class=HTMLResponse, summary="Новый пароль: форма")
async def reset_page(token: str) -> Response:
    invitation = await staff_password_reset.describe_password_reset(token)
    if invitation is None:
        return _invalid_page()
    return _form_page(invitation, token)


@router.post("/{token}", response_class=HTMLResponse, summary="Сохранить новый пароль и войти")
async def reset_submit(
    request: Request,
    token: str,
    password: Annotated[str, Form()] = "",
) -> Response:
    """Новый пароль → прежние сессии сотрудника погашены → вход и главная."""
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    invitation = await staff_password_reset.describe_password_reset(token)
    if invitation is None:
        return _invalid_page()
    if not password:
        return _form_page(invitation, token, error="Введите пароль.", status_code=422)
    try:
        accepted = await staff_password_reset.accept_password_reset(token, password=password)
    except AppError as error:
        if error.code == staff_password_reset.ERR_AUTH_RESET_INVALID:
            return _invalid_page()
        return _form_page(
            invitation,
            token,
            error=browser.AUTH_ERROR_MESSAGES.get(
                error.code, "Не получилось сохранить пароль. Попробуйте ещё раз."
            ),
            status_code=error.status_code,
        )
    try:
        grant = await staff_auth.login(
            accepted.tenant_id, accepted.login, password, client_ip=client_ip(request)
        )
    except AppError as error:
        logger.warning(
            "staff.password_reset_login_failed",
            code=error.code,
            user_id=str(accepted.user_id),
            tenant_id=str(accepted.tenant_id),
        )
        return browser.html_page(
            render_page(
                "reset_done.html",
                tenant_name=invitation.tenant_name,
                staff_login=accepted.login,
                login_url=browser.hotel_login_path(accepted.tenant_slug),
                reason=browser.AUTH_ERROR_MESSAGES.get(error.code),
            )
        )
    previous_token = request.cookies.get(staff_auth.STAFF_SESSION_COOKIE)
    if previous_token:
        await staff_auth.logout(previous_token)
    response: Response = RedirectResponse(f"/staff/{accepted.tenant_slug}", status_code=303)
    browser.set_session_cookie(response, grant.session_token)
    browser.set_hotel_cookie(response, accepted.tenant_slug)
    return response
