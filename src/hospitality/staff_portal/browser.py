"""Браузерная обвязка страниц кабинета: заголовки, CSRF-щит форм, cookie, тексты отказов.

Выделено из `router.py` в PR F серии #48 (R-3): ровно те же правила нужны
второму роутеру страниц — анонимным страницам приглашения (`invites.py`), а
импорт из `router.py` замкнул бы цикл (`router.py` включает их роутер). Сами
контракты не менялись; их описание живёт в докстринге `router.py` (CSRF,
кэширование, CSP) и в README пакета.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote, urlsplit

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse

from hospitality.platform import staff_auth
from hospitality.platform.staff_auth import STAFF_SESSION_COOKIE
from hospitality.platform.staff_credentials import (
    ERR_AUTH_LOGIN_INVALID,
    ERR_AUTH_LOGIN_RATE_LIMITED,
    ERR_AUTH_PASSWORD_TOO_SHORT,
)
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
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
# Единственное послабление — картинки ещё и `blob:`: предпросмотр выбранного фото
# (`URL.createObjectURL`). Только две страницы: «Товары и меню» (spec 0038 PR 7,
# `catalog_photo.js` — до загрузки) и демо «Забытые вещи» (#604,
# `demo_lost_found.js` — фото не покидает браузер). blob-адрес создаёт только
# скрипт самой страницы, а скрипт по-прежнему — только файл `/staff/static`;
# inline и чужие источники закрыты, как везде.
PHOTO_PREVIEW_PAGE_CSP: Final = "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'"

# Код каталога ошибок (docs/runbooks/errors.md, R-8): JSON-действие кабинета
# отклонено CSRF-щитом (не-JSON Content-Type, нет Origin или он чужой).
ERR_STAFF_CSRF_REJECTED: Final = "ERR-AUTH-009"

# Сообщения форм аутентификации по кодам каталога ошибок: пользователю —
# по-русски и без деталей (тексты — spec 0037 §7). Один текст на «нет логина»
# и «неверный пароль» — перечисление логинов запрещено. ERR-AUTH-012 —
# подсказка формата: формат не секрет, а логин в русской раскладке — самая
# частая ошибка ввода.
AUTH_ERROR_MESSAGES: Final[dict[str, str]] = {
    staff_auth.ERR_AUTH_INVALID_CREDENTIALS: "Неверный логин или пароль.",
    ERR_AUTH_LOGIN_INVALID: (
        "Логин — латинские буквы и цифры, от 3 до 12 знаков, первой — буква. "
        "Проверьте раскладку клавиатуры."
    ),
    staff_auth.ERR_AUTH_USER_DEACTIVATED: (
        "Учётная запись деактивирована — обратитесь к менеджеру."
    ),
    ERR_AUTH_LOGIN_RATE_LIMITED: (
        "Слишком много неудачных попыток входа. Подождите несколько минут и попробуйте снова."
    ),
    ERR_AUTH_PASSWORD_TOO_SHORT: "Пароль должен быть не короче 8 символов.",
}

# Cookie «отель этого браузера» (spec 0037 §3): общий вход `/staff/login` по
# нему ведёт сразу на страницу входа отеля. Значение — slug, не секрет;
# ставится только успешным входом, поэтому опечатка в коде не «залипает».
STAFF_HOTEL_COOKIE: Final = "staff_hotel"
_STAFF_HOTEL_COOKIE_MAX_AGE: Final = 365 * 86400


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


def set_hotel_cookie(response: Response, tenant_slug: str) -> None:
    """Запомнить отель браузера на год (spec 0037 §3). Выход её не стирает:
    следующий вход на общем компьютере начинается со страницы того же отеля."""
    response.set_cookie(
        STAFF_HOTEL_COOKIE,
        tenant_slug,
        max_age=_STAFF_HOTEL_COOKIE_MAX_AGE,
        path="/staff",
        httponly=True,
        secure=True,
        samesite="lax",
    )


def hotel_login_path(tenant_slug: str, *, switch: bool = False) -> str:
    """Адрес страницы входа отеля; `switch` — признак «войти под другим
    логином» в обход 303 в кабинет (spec 0037 §3, ссылка с экрана 403).

    Slug экранируется: на 401 он приходит из пути запроса, а не из БД."""
    path = f"/staff/{quote(tenant_slug, safe='')}/login"
    return f"{path}?switch=1" if switch else path


def require_json_same_origin(request: Request) -> None:
    """CSRF-щит JSON-действий (докстринг `api_router.py`); нарушение — 403 в конверте.

    В отличие от HTML-форм (`is_cross_origin`) отсутствующий Origin
    ЗДЕСЬ отказ: fetch шлёт Origin на POST всегда, а не-браузерным клиентам
    в кабинете делать нечего (сервисный API — `/api/v1/*` с токеном).

    Непрозрачный `Origin: null` — разбор в `is_cross_origin`; здесь
    он даёт тот же 403, пока `Host` есть (пустой `netloc` с ним не совпадёт).
    """
    _require_same_origin(request, "application/json")


def require_upload_same_origin(request: Request) -> None:
    """CSRF-щит загрузки файла из кабинета (фото товара, spec 0038 PR 7) — тот же 403.

    Тело — `multipart/form-data`: его чужой сайт отправить может (простой
    запрос CORS, без предварительной проверки), поэтому решает только Origin —
    тот же, что у JSON-действий, и так же обязателен.
    """
    _require_same_origin(request, "multipart/form-data")


def _require_same_origin(request: Request, expected_content_type: str) -> None:
    content_type = request.headers.get("content-type", "")
    origin = request.headers.get("origin", "")
    # Сравнивается только netloc, БЕЗ схемы (осознанно, рекомендация 3 ревью
    # PR #154): за Cloudflare-туннелем uvicorn пока видит http (#149,
    # --proxy-headers), полное сравнение дало бы ложные 403 всем живым
    # пользователям staging. Downgrade-подмену http-Origin закрывают Secure
    # cookie + schemeful SameSite; ужесточить вместе с фиксом #149.
    same_origin = bool(origin) and urlsplit(origin).netloc == request.headers.get("host", "")
    if content_type.lower().partition(";")[0].strip() != expected_content_type or not same_origin:
        logger.warning(
            "staff.action_csrf_rejected",
            content_type=content_type,
            origin=origin,
        )
        raise AppError(
            code=ERR_STAFF_CSRF_REJECTED,
            message=(
                f"Staff action requires {expected_content_type} and a same-origin Origin header"
            ),
            status_code=403,
        )
