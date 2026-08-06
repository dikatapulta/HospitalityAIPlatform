"""Браузерная обвязка страниц кабинета: заголовки, CSRF-щит форм, cookie, тексты отказов.

Выделено из `router.py` в PR F серии #48 (R-3): ровно те же правила нужны
второму роутеру страниц — анонимным страницам приглашения (`invites.py`), а
импорт из `router.py` замкнул бы цикл (`router.py` включает их роутер). Сами
контракты не менялись; их описание живёт в докстринге `router.py` (CSRF,
кэширование, CSP) и в README пакета.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

from hospitality.platform import staff_auth
from hospitality.platform.staff_auth import STAFF_SESSION_COOKIE
from hospitality.platform.staff_credentials import (
    ERR_AUTH_LOGIN_RATE_LIMITED,
    ERR_AUTH_PASSWORD_TOO_SHORT,
)
from hospitality.platform.staff_team import ERR_AUTH_SELF_ACTION
from hospitality.shared.config import get_settings
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Аутентифицированный HTML: не кэшировать нигде, не встраивать во фреймы
# (clickjacking на кнопках действий), не отдавать наружу путь страницы. CSP
# default-src 'self' — свой JS только файлом из /staff/static, inline и чужие
# источники запрещены (ревью PR #153: ужесточено с появлением своего JS).
#
# `Referrer-Policy: strict-origin`, а НЕ `no-referrer` (issue #164) — это не
# послабление, а единственное значение, при котором обе защиты работают разом:
#
# - `Referer` и так не унесёт секрет: `strict-origin` шлёт ОДИН источник, без
#   пути, поэтому токен приглашения `/staff/invite/{token}` наружу не уходит —
#   ровно то, ради чего ставился `no-referrer` (ревью PR #155/#159);
# - а вот `no-referrer` ломал вход: по Fetch (§ append a request Origin header)
#   запрос в режиме навигации — то есть ЛЮБАЯ отправка HTML-формы — при этой
#   политике обязан прислать `Origin: null`. Щит ниже видел непрозрачный
#   источник и отвечал 403 на собственные логин, логаут, заселение и принятие
#   приглашения. Кабинет не пускал никого; нашлось первым живым прогоном на
#   staging (#164) — httpx браузерную связку не воспроизводит.
#
# Обнулять `Origin` `strict-origin` умеет только при понижении https → http,
# чего у браузера не бывает: снаружи обе стороны https (TLS терминирует
# Cloudflare), локально обе — http. Кто пойдёт «ужесточать» обратно — сначала
# прочитайте `test_page_referrer_policy_does_not_null_form_origin`.
PAGE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "Referrer-Policy": "strict-origin",
}

# Сообщения форм аутентификации по кодам каталога ошибок: пользователю —
# по-русски и без деталей. Один текст на «нет учётки» и «неверный пароль» —
# перечисление email запрещено (тот же контракт у формы входа и у формы
# принятия приглашения: инвайт не должен становиться оракулом, PR #148).
# ERR-AUTH-007 здесь ничего не выдаёт: длина пароля проверяется ДО поиска
# личности, поэтому короткий пароль отвечает одинаково на занятый и свободный
# email (`staff_credentials.ensure_password_policy`, ревью PR #159).
AUTH_ERROR_MESSAGES: Final[dict[str, str]] = {
    staff_auth.ERR_AUTH_INVALID_CREDENTIALS: "Неверный email или пароль.",
    staff_auth.ERR_AUTH_USER_DEACTIVATED: (
        "Учётная запись деактивирована — обратитесь к менеджеру."
    ),
    ERR_AUTH_LOGIN_RATE_LIMITED: (
        "Слишком много неудачных попыток входа. Подождите несколько минут и попробуйте снова."
    ),
    ERR_AUTH_PASSWORD_TOO_SHORT: "Пароль должен быть не короче 8 символов.",
    # Виден только на форме принятия приглашения и только тому, кто уже доказал
    # пароль менеджера этого отеля: «повторяйте попытку» здесь было бы враньём.
    ERR_AUTH_SELF_ACTION: (
        "Вы уже менеджер этого отеля — приглашение не может понизить вашу роль. "
        "Передайте ссылку тому, кого приглашаете."
    ),
}


def html_page(html: str, *, status_code: int = 200) -> HTMLResponse:
    """Единственный способ отдать HTML кабинета (заголовки — `PAGE_HEADERS`)."""
    return HTMLResponse(html, status_code=status_code, headers=PAGE_HEADERS)


def is_cross_origin(request: Request) -> bool:
    """CSRF-щит HTML-форм (докстринг `router.py`).

    `Origin` шлют все современные браузеры на POST; отсутствие заголовка
    (curl, смоук-тесты) не считается кросс-сайтом — это оборона в глубину
    поверх SameSite=Lax, а не единственная граница.

    Непрозрачный источник `Origin: null` (форма из песочного iframe, документ
    после кросс-доменного редиректа) — наоборот, ОТКАЗ, а не «источника нет»:
    значение подделывается, а на логине cookie-сессии ещё нет, поэтому
    SameSite не подстрахует (login-CSRF, #160). Своя страница `null` присылать
    не должна — за это отвечает `Referrer-Policy` в `PAGE_HEADERS` выше;
    прежняя политика присылала, и щит резал собственный вход (#164).
    """
    origin = request.headers.get("origin")
    if not origin:
        return False
    if origin == "null":
        return True
    return urlsplit(origin).netloc != request.headers.get("host", "")


def cross_origin_rejected(request: Request) -> Response:
    # Браузерная граница до чтения формы — вне конверта ошибок R-8 (осознанно):
    # это не API-ответ клиенту, а отказ навигации; диагноз — по лог-событию.
    logger.warning("staff.cross_origin_rejected", origin=request.headers.get("origin", ""))
    return PlainTextResponse("Запрос отклонён: чужой источник (CSRF).", status_code=403)


def set_session_cookie(response: Response, token: str) -> None:
    """Cookie сессии кабинета — контракт ревью PR #148 (spec 0033 §3.3).

    Max-Age = absolute TTL: idle-срок и отзыв всё равно проверяет сервер на
    каждом запросе (`resolve_staff_session`) — атрибуты cookie не граница
    безопасности, как и в гостевом канале.
    """
    response.set_cookie(
        STAFF_SESSION_COOKIE,
        token,
        max_age=get_settings().staff_session_absolute_ttl_days * 86400,
        path="/staff",
        httponly=True,
        secure=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        STAFF_SESSION_COOKIE, path="/staff", httponly=True, secure=True, samesite="lax"
    )
