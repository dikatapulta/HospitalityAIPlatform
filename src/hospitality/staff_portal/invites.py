"""Страницы приглашения сотрудника `/staff/invite/{token}` (spec 0033 §3.4, PR F).

Единственная АНОНИМНАЯ поверхность кабинета: ссылку открывает человек, у
которого ещё нет ни сессии, ни членства, ни тенанта в контексте запроса
(`invite` — служебный сегмент `_STAFF_RESERVED_SEGMENTS`, звено
`TenantResolver` его не резолвит). Поэтому маршруты живут отдельным роутером,
а не рядом со страницами `/staff/{tenant_slug}/…`, где всё проходит через
`_page_context`; общая браузерная обвязка — `browser.py`.

Токен ссылки — секрет в пути: он маскируется в access-логе и Sentry
(`shared/logging._SECRET_PATH_PATTERNS`), как токен гостевой bind-ссылки.

Безопасность формы принятия:

- CSRF — тот же щит, что у логина (`browser.is_cross_origin`): cookie-сессии
  здесь ещё нет, SameSite не помогает, и принятие инвайта создаёт сессию —
  проверка `Origin` остаётся единственной браузерной границей.
- Перечисление email запрещено и здесь: существующий email отвечает тем же
  ERR-AUTH-001, что и форма входа, а бюджет попыток общий с логином
  (`staff_credentials.enforce_login_rate_limit`, блокер ревью PR #148).
- Согласие на обработку ПД — та же строка, что видит гость (spec 0033 §3.4):
  текст берётся из канона `channels/common/consent.py`, а не пишется заново —
  разъехавшиеся тексты согласия это юридический дефект. Факт согласия
  сотрудника фиксируется версией в лог-событии `staff.invite_accepted`;
  отдельной колонки у `users` в v1 нет (согласие даётся один раз при
  заведении учётки).
"""

from __future__ import annotations

import html
from typing import Annotated, Final

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from hospitality.channels.common.consent import CONSENT_VERSION, consent_text
from hospitality.platform import staff_auth, staff_invites
from hospitality.platform.legal import privacy_policy_url
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import browser, team
from hospitality.staff_portal.rendering import render_page

logger = get_logger(module=__name__)

router = APIRouter(prefix="/invite", tags=["staff-portal"])

# Язык кабинета — русский (§7 FOUNDATION): сотруднику показывается ru-строка
# согласия, гостю — все языки. Текст один и тот же, источник один и тот же.
_CONSENT_LANGUAGE: Final = "ru"


def _consent_html() -> str:
    """Строка согласия со ссылкой-якорем (канон `channels/web/page.py`).

    Текст экранируется целиком, и только адрес политики заменяется на ссылку:
    в шаблон уходит готовый безопасный HTML.
    """
    url = privacy_policy_url()
    escaped_url = html.escape(url)
    link = f'<a href="{escaped_url}" target="_blank" rel="noopener">{escaped_url}</a>'
    return html.escape(consent_text(_CONSENT_LANGUAGE)).replace(escaped_url, link)


def _invalid_page() -> Response:
    """Истёкшая, отозванная, использованная и несуществующая ссылка — один экран
    (причины держателю ссылки неразличимы, ERR-AUTH-004)."""
    return browser.html_page(render_page("invite_invalid.html"), status_code=410)


def _form_page(
    invitation: staff_invites.InviteInvitation,
    token: str,
    *,
    email: str = "",
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return browser.html_page(
        render_page(
            "invite.html",
            tenant_name=invitation.tenant_name,
            invited_name=invitation.invited_name,
            role_label=team.role_label(invitation.role_key),
            action=f"/staff/invite/{token}",
            consent_html=_consent_html(),
            email=email,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/{token}", response_class=HTMLResponse, summary="Приглашение: форма принятия")
async def invite_page(token: str) -> Response:
    invitation = await staff_invites.describe_invite(token)
    if invitation is None:
        return _invalid_page()
    return _form_page(invitation, token)


@router.post("/{token}", response_class=HTMLResponse, summary="Принять приглашение и войти")
async def invite_submit(
    request: Request,
    token: str,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Принятие → сразу вход и очередь заявок (spec 0033 §3.4: «попадает в очередь»).

    Сессия создаётся каноническим `staff_auth.login`, а не вторым способом
    выпуска токена (P-12): цена — одна лишняя проверка argon2 раз в жизни
    сотрудника. Дефолты полей пустые — как у логина: урезанный ручной POST
    должен получить ту же HTML-форму, а не JSON-конверт 422.
    """
    if browser.is_cross_origin(request):
        return browser.cross_origin_rejected(request)
    invitation = await staff_invites.describe_invite(token)
    if invitation is None:
        return _invalid_page()
    if not email or not password:
        return _form_page(
            invitation, token, email=email, error="Введите email и пароль.", status_code=422
        )
    client_ip = browser.client_ip(request)
    try:
        accepted = await staff_invites.accept_invite(
            token, email=email, password=password, client_ip=client_ip
        )
    except AppError as error:
        if error.code == staff_invites.ERR_AUTH_INVITE_INVALID:
            return _invalid_page()
        return _form_page(
            invitation,
            token,
            email=email,
            error=browser.AUTH_ERROR_MESSAGES.get(
                error.code, "Не получилось принять приглашение. Попробуйте ещё раз."
            ),
            status_code=error.status_code,
        )
    logger.info(
        "staff.invite_consent_recorded",
        consent_version=CONSENT_VERSION,
        user_id=str(accepted.user_id),
        tenant_id=str(accepted.tenant_id),
    )
    grant = await staff_auth.login(email, password, client_ip=client_ip)
    response: Response = RedirectResponse(_landing_path(grant, accepted), status_code=303)
    browser.set_session_cookie(response, grant.session_token)
    return response


def _landing_path(
    grant: staff_auth.StaffSessionGrant, accepted: staff_invites.InviteAcceptResult
) -> str:
    """Куда отправить принявшего: очередь отеля из инвайта.

    Slug берётся из членств свежего логина (в них он уже есть) — второго
    запроса к платформе не нужно. Членство принятого тенанта только что создал
    `accept_invite`, поэтому совпадение гарантировано; фолбэк на выбор отеля
    честнее 500-ки, если членство успели отозвать между двумя запросами.
    """
    for membership in grant.memberships:
        if membership.tenant_id == accepted.tenant_id:
            return f"/staff/{membership.tenant_slug}/requests"
    return "/staff/"
