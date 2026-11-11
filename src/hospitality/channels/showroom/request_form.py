"""Формы запроса 3D-витрины и `POST /showroom/{slug}/requests` (spec 0069 §2.4, §4, §5;
PR (в) серии, #805).

Две формы на одной странице — «Для групп» (служба продаж) и «Для гостей» (служба
бронирования) — и карточка «Проверьте запрос», которую открывает черновик помощника
(`DraftRequest`, PR (б)): событие `showroom:draft` из `assistant.js`. Блок
встаёт в слот страницы `[data-sr-slot=request]` (`showroom.js`, PR (а)), вкладку берёт из
`showroom:audience`, номер, выбранный на модели, — из `showroom:select`, виды и типы
номеров — из сцены страницы (`window.Showroom.scene`). Отправляет только кнопка
посетителя (P-9): у модели инструмента, создающего запрос, нет.

Порядок проверок запроса: витрина не видна снаружи — 404 `001`, нет доступа — 403 `005`
(`access.py`, PR (а)); заполнено скрытое поле-ловушка `website` — 200 без записи
(боты); тело вне правил — 422 `004`; лимиты `showroom_request_ip` 5 в час на адрес и
`showroom_request_tenant` 50 в сутки отеля — 429 `003` (fail-open, канон spec 0023);
дальше — `showroom.api.create_request`: 201 новый, 200 повтор ключа. Тело — JSON; в лог
не пишется ничего из формы.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Request, Response, status
from pydantic import BaseModel, ValidationError

from hospitality.channels.showroom import access, assets
from hospitality.channels.showroom.consent import SHOWROOM_CONSENT_VERSION
from hospitality.channels.showroom.page import config_json
from hospitality.channels.showroom.request_texts import LANGUAGES, form_texts
from hospitality.modules.showroom import api as showroom_api
from hospitality.platform.config import load_tenant_config
from hospitality.shared.clientip import client_ip
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import MAX_VALIDATION_DETAILS, AppError, ErrorResponse, clip_loc
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Без префикса: роутер витрины (`router.py`, префикс /showroom) подключает этот.
router = APIRouter(tags=["showroom"])

# Лимиты §2.4: ступень по адресу первой — аноним не должен гонять БД.
_IP_SCOPE: Final = "showroom_request_ip"
_IP_LIMIT: Final = 5
_IP_WINDOW_SECONDS: Final = 3600
_TENANT_SCOPE: Final = "showroom_request_tenant"
_TENANT_LIMIT: Final = 50
_TENANT_WINDOW_SECONDS: Final = 86400

_HONEYPOT_FIELD: Final = "website"
JS_FILE: Final = "request_form.js"
CSS_FILE: Final = "request_form.css"


class ShowroomRequestAccepted(BaseModel):
    """Ответ посетителю: только «отправлено» — ни номера заявки, ни её id."""

    status: str = "sent"


_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    404: {"model": ErrorResponse, "description": "Витрина не видна снаружи (001)"},
    403: {"model": ErrorResponse, "description": "Нет доступа к странице по коду (005)"},
    422: {"model": ErrorResponse, "description": "Запрос вне правил (004)"},
    429: {"model": ErrorResponse, "description": "Лимит запросов с адреса или отеля (003)"},
}


@router.post(
    "/{tenant_slug}/requests",
    status_code=status.HTTP_201_CREATED,
    summary="Отправить запрос группы или гостя; повтор ключа — 200",
    responses=_RESPONSES,
)
async def send_request(
    tenant_slug: str,
    request: Request,
    response: Response,
    body: Annotated[dict[str, Any], Body()],
) -> ShowroomRequestAccepted:
    showroom = await access.open_showroom(tenant_slug)
    access.require_access(showroom, request)
    with tenant_context(showroom.tenant_id):
        if body.pop(_HONEYPOT_FIELD, None):
            logger.info("showroom_request_trap")
            response.status_code = status.HTTP_200_OK
            return ShowroomRequestAccepted()
        data = _validated(body)
        await _limit(_IP_SCOPE, client_ip(request), _IP_LIMIT, _IP_WINDOW_SECONDS)
        await _limit(_TENANT_SCOPE, str(showroom.tenant_id), _TENANT_LIMIT, _TENANT_WINDOW_SECONDS)
        placed = await showroom_api.create_request(data, consent_version=SHOWROOM_CONSENT_VERSION)
    if placed.duplicate:
        response.status_code = status.HTTP_200_OK
    return ShowroomRequestAccepted()


def _validated(body: dict[str, Any]) -> showroom_api.ShowroomRequestCreate:
    """Тело → схема модуля; ошибка — 422 `004` с путями полей, без значений (там контакты)."""
    try:
        return showroom_api.ShowroomRequestCreate.model_validate(body)
    except ValidationError as error:
        # Не больше `MAX_VALIDATION_DETAILS` путей, имена усечены: тело из тысяч лишних
        # ключей иначе ложилось бы в лог и в `message` целиком (issue #968).
        violations = error.errors(include_url=False, include_input=False, include_context=False)
        fields = sorted(
            {
                ".".join(str(part) for part in clip_loc(item["loc"]))
                for item in violations[:MAX_VALIDATION_DETAILS]
            }
        )
        logger.info("showroom_request_invalid", fields=fields, error_count=len(violations))
        raise AppError(
            code=showroom_api.ERR_SHOWROOM_REQUEST_INVALID,
            message=f"Request is out of the form rules: {', '.join(fields) or 'body'}",
            status_code=422,
        ) from None


async def _limit(scope: str, key: str, limit: int, window_seconds: int) -> None:
    decision = await consume_rate_limit(scope, key, limit=limit, window_seconds=window_seconds)
    if decision.available and not decision.allowed:
        # Ключ ступени по адресу — адрес посетителя: в лог не пишется (issue #173).
        logger.warning(
            "showroom_request_rate_limited",
            error_code=showroom_api.ERR_SHOWROOM_RATE_LIMITED,
            scope=scope,
            count=decision.count,
            limit=decision.limit,
        )
        raise AppError(
            code=showroom_api.ERR_SHOWROOM_RATE_LIMITED,
            message="Too many showroom requests",
            status_code=429,
        )


async def page_block(showroom: access.Showroom) -> str:
    """Блок форм для страницы посетителя: «сегодня» — по поясу отеля (§2.4)."""
    with tenant_context(showroom.tenant_id):
        async with session_scope() as session:
            config = await load_tenant_config(session, showroom.tenant_id)
    return form_block(
        tenant_slug=showroom.slug,
        today=utc_now().astimezone(config.tzinfo).date(),
        booking_max_guests=showroom.settings.booking_max_guests,
    )


def form_block(*, tenant_slug: str, today: date, booking_max_guests: int) -> str:
    """Контейнер, данные и файлы блока форм.

    Форму рисует `request_form.js` из JSON: надписи трёх языков (язык — `html[lang]`),
    сегодня отеля и предел дат (заезд не раньше сегодня, выезд не дальше 400 дней),
    пределы гостей и номеров. Скрипт и стили — статика витрины (`assets.py`): CSP
    страницы встроенных не пускает.
    """
    data = {
        "slug": tenant_slug,
        "today": today.isoformat(),
        "max_days_ahead": showroom_api.MAX_DAYS_AHEAD,
        "max_group_guests": showroom_api.MAX_GROUP_GUESTS,
        "max_booking_guests": booking_max_guests,
        "max_booking_rooms": showroom_api.MAX_BOOKING_ROOMS,
        "max_group_rooms": showroom_api.MAX_GROUP_ROOMS,
        "texts": {language: form_texts(language) for language in LANGUAGES},
    }
    return (
        f'<link rel="stylesheet" href="{assets.url(CSS_FILE)}">'
        '<section class="sr-request" id="showroom-request" aria-live="polite"></section>'
        f'<script type="application/json" id="showroom-request-data">{config_json(data)}</script>'
        f'<script src="{assets.url(JS_FILE)}" defer></script>'
    )
