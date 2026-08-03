"""JSON-действия кабинета персонала (spec 0033 §5–§7): очередь заявок
(взять / готово / отменить, PR D), карточка заселения (bind-ссылка,
перевыпуск кода, переселение, продление, выезд, счётчик привязок, PR E) и
состав команды (пригласить, отозвать ссылку, сменить роль, отключить, PR F).

Действия зовёт ванильный JS страниц (`static/queue.js`, `static/checkin.js`,
`static/team.js`).
Авторизация — `staff_auth.require_role` НАПРЯМУЮ (JSON-исходы: 401/403 уходят
каноническим конвертом ошибок R-8, без браузерных редиректов `_page_context`);
require_role же сверяет RLS-контекст запроса с тенантом действия (fail-closed,
ревью PR #153).

CSRF-контракт JSON-действий (докстринг `router.py`, README пакета): каждый POST
обязан нести `Content-Type: application/json` И НЕПУСТОЙ same-origin `Origin`
(fetch шлёт его всегда) — кросс-сайтовая форма не умеет JSON-тип, кросс-сайтовый
fetch не пройдёт по Origin. Нарушение — 403 `ERR-AUTH-009`. Отдельный CSRF-токен
при этом не нужен. Форма с enctype=text/plain, подделывающая JSON-тело, до
обработчика не доходит: без JSON Content-Type FastAPI не парсит тело → 422.

Сами переходы — `requests_api.change_request_status` (P-5): та же карта
`STATUS_TRANSITIONS`, те же события и уведомления гостю, что из Telegram;
конфликт (взяли раньше) — 409 `ERR-REQUESTS-003`, страница показывает
«уже взята» и обновляет список.
"""

from __future__ import annotations

import uuid
from typing import Final
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_auth, staff_invites, staff_team
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.staff_portal import checkin, team

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8): JSON-действие кабинета
# отклонено CSRF-щитом (не-JSON Content-Type, нет Origin или он чужой) и
# превышенный лимит выпуска bind-ссылок по (tenant, stay).
ERR_STAFF_CSRF_REJECTED = "ERR-AUTH-009"
ERR_BIND_LINK_RATE_LIMITED = "ERR-AUTH-010"

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Очередь: смотреть/взять/готово/отменить может любая роль (мини-матрица §3.2).
_any_role: Final = staff_auth.require_role(
    StaffRole.STAFF, StaffRole.RECEPTIONIST, StaffRole.MANAGER
)
# Заселение и карточки Stay — только ресепшен и менеджер (мини-матрица §3.2).
_reception_role: Final = staff_auth.require_role(StaffRole.RECEPTIONIST, StaffRole.MANAGER)
# Состав команды — только менеджер (мини-матрица §3.2, развилка Ф-3 spec 0033).
_manager_role: Final = staff_auth.require_role(StaffRole.MANAGER)


class CompleteBody(BaseModel):
    """«Готово»: примечание опционально (частичное выполнение, spec 0021/0030)."""

    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def _strip_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class CancelBody(BaseModel):
    """«Отменить»: причина обязательна (spec 0033 §5) — пустая строка не проходит."""

    note: str = Field(min_length=1, max_length=500)

    @field_validator("note")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("cancellation note must not be blank")
        return stripped


def _require_json_same_origin(request: Request) -> None:
    """CSRF-щит JSON-действий (см. докстринг модуля); нарушение — 403 в конверте.

    В отличие от HTML-форм (`browser.is_cross_origin`) отсутствующий Origin
    ЗДЕСЬ отказ: fetch шлёт Origin на POST всегда, а не-браузерным клиентам
    в кабинете делать нечего (сервисный API — `/api/v1/*` с токеном).

    Непрозрачный `Origin: null` — разбор в `browser.is_cross_origin`; здесь он
    даёт тот же 403 (пустой `netloc` не совпадёт с `Host`).
    """
    content_type = request.headers.get("content-type", "")
    origin = request.headers.get("origin", "")
    # Сравнивается только netloc, БЕЗ схемы (осознанно, рекомендация 3 ревью
    # PR #154): за Cloudflare-туннелем uvicorn пока видит http (#149,
    # --proxy-headers), полное сравнение дало бы ложные 403 всем живым
    # пользователям staging. Downgrade-подмену http-Origin закрывают Secure
    # cookie + schemeful SameSite; ужесточить вместе с фиксом #149.
    same_origin = bool(origin) and urlsplit(origin).netloc == request.headers.get("host", "")
    if content_type.lower().partition(";")[0].strip() != "application/json" or not same_origin:
        logger.warning(
            "staff.action_csrf_rejected",
            content_type=content_type,
            origin=origin,
        )
        raise AppError(
            code=ERR_STAFF_CSRF_REJECTED,
            message="Staff action requires application/json and a same-origin Origin header",
            status_code=403,
        )


async def _authorized_actor(request: Request) -> staff_auth.StaffContext:
    _require_json_same_origin(request)
    return await _any_role(request)


def _acting_user(context: staff_auth.StaffContext) -> requests_api.ActingUser:
    return requests_api.ActingUser(user_id=context.user_id, display_name=context.display_name)


@router.post("/requests/{request_id}/claim", summary="Взять заявку в работу")
async def claim_request(request: Request, request_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    """`new → in_progress`, пишет «кто взял» (claimed_by, spec 0033 §5).

    Тела у действия нет (JS шлёт `{}` ради Content-Type из CSRF-контракта);
    конфликт двух взявших разрешает карта переходов — второй получает 409.
    """
    context = await _authorized_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.IN_PROGRESS,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=_acting_user(context),
    )
    logger.info(
        "staff.request_claimed",
        request_id=str(request_id),
        user_id=str(context.user_id),
    )
    return updated


@router.post("/requests/{request_id}/complete", summary="Заявка выполнена")
async def complete_request(
    request: Request, request_id: uuid.UUID, body: CompleteBody
) -> requests_api.ServiceRequestRead:
    context = await _authorized_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.DONE,
        resolution_note=body.note,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=_acting_user(context),
    )
    logger.info(
        "staff.request_completed",
        request_id=str(request_id),
        user_id=str(context.user_id),
        with_note=body.note is not None,
    )
    return updated


@router.post("/requests/{request_id}/cancel", summary="Отменить заявку (причина обязательна)")
async def cancel_request(
    request: Request, request_id: uuid.UUID, body: CancelBody
) -> requests_api.ServiceRequestRead:
    context = await _authorized_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.CANCELLED,
        resolution_note=body.note,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=_acting_user(context),
    )
    logger.info(
        "staff.request_cancelled",
        request_id=str(request_id),
        user_id=str(context.user_id),
    )
    return updated


# ---------------------------------------------------------------------------
# Карточка заселения (spec 0033 §6, PR E): действия над Stay.
# ---------------------------------------------------------------------------


class MoveBody(BaseModel):
    """«Переселить»: новая комната; занятая — 409 ERR-GUESTS-002."""

    room_number: str = Field(min_length=1, max_length=20)

    @field_validator("room_number")
    @classmethod
    def _strip_room(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("room number must not be blank")
        return stripped


class ExtendBody(BaseModel):
    """«Продлить»: на сколько ночей вперёд (расчёт локального времени — сервер)."""

    nights: int = Field(ge=1, le=checkin.MAX_NIGHTS)


class BindLinkView(BaseModel):
    """Свежая одноразовая bind-ссылка: QR и URL показываются один раз."""

    bind_url: str
    qr_svg: str
    expires_in_seconds: int


class ReissuedCodeView(BaseModel):
    """Новый код заселения — показывается один раз (в БД только хэш)."""

    access_code: str


class StayActionView(BaseModel):
    """Итог действия над Stay для обновления карточки без перерисовки."""

    room_number: str
    check_out_local: str


class BindingsCountView(BaseModel):
    """Счётчик привязок Stay — поллинг индикатора «гость подключился» (3 с)."""

    count: int


async def _authorized_receptionist(request: Request) -> staff_auth.StaffContext:
    _require_json_same_origin(request)
    return await _reception_role(request)


async def _enforce_bind_link_issue_limit(
    context: staff_auth.StaffContext, stay_id: uuid.UUID
) -> None:
    """Rate-limit выпуска bind-ссылок по (tenant, stay) (spec 0033 §9, канон 0023):
    человек столько не нажмёт — лимит ловит залипший скрипт."""
    settings = get_settings()
    limit = settings.guest_bind_link_issue_rate_limit_attempts
    if limit <= 0:
        return
    decision = await consume_rate_limit(
        "guest_bind_link_issue",
        f"{context.tenant_id}:{stay_id}",
        limit=limit,
        window_seconds=settings.guest_bind_link_issue_rate_limit_window_seconds,
    )
    if decision.available and not decision.allowed:
        logger.warning(
            "staff.bind_link_rate_limited",
            stay_id=str(stay_id),
            user_id=str(context.user_id),
            count=decision.count,
            limit=decision.limit,
        )
        raise AppError(
            code=ERR_BIND_LINK_RATE_LIMITED,
            message="Too many bind links for this stay — wait a minute and retry",
            status_code=429,
        )


@router.post("/stays/{stay_id}/bind-link", summary="Выпустить одноразовую QR-ссылку привязки")
async def issue_stay_bind_link(request: Request, stay_id: uuid.UUID) -> BindLinkView:
    """Старые ссылки не гасятся — доживают свой TTL (120 с); повторный выпуск
    безопасен (spec 0033 §6). Redis недоступен — 503 ERR-GUESTS-005, гость
    входит по коду."""
    context = await _authorized_receptionist(request)
    await _enforce_bind_link_issue_limit(context, stay_id)
    token = await guests_api.issue_bind_link(stay_id)
    url = checkin.bind_link_url(context.tenant_slug, token)
    logger.info("staff.bind_link_issued", stay_id=str(stay_id), user_id=str(context.user_id))
    return BindLinkView(
        bind_url=url,
        qr_svg=checkin.qr_svg(url),
        expires_in_seconds=guests_api.BIND_LINK_TTL_SECONDS,
    )


@router.post("/stays/{stay_id}/reissue-code", summary="Перевыпустить код заселения")
async def reissue_stay_code(request: Request, stay_id: uuid.UUID) -> ReissuedCodeView:
    context = await _authorized_receptionist(request)
    code = await guests_api.reissue_access_code(stay_id)
    logger.info("staff.stay_code_reissued", stay_id=str(stay_id), user_id=str(context.user_id))
    return ReissuedCodeView(access_code=guests_api.format_access_code(code))


@router.post("/stays/{stay_id}/move", summary="Переселить гостя в другую комнату")
async def move_stay(request: Request, stay_id: uuid.UUID, body: MoveBody) -> StayActionView:
    context = await _authorized_receptionist(request)
    stay = await guests_api.move_stay(stay_id, body.room_number)
    logger.info(
        "staff.stay_moved",
        stay_id=str(stay_id),
        room_number=stay.room_number,
        user_id=str(context.user_id),
    )
    zone = await checkin.hotel_zone(context)
    return StayActionView(
        room_number=stay.room_number,
        check_out_local=checkin.format_local(stay.check_out_at, zone),
    )


@router.post("/stays/{stay_id}/extend", summary="Продлить проживание на N ночей")
async def extend_stay(request: Request, stay_id: uuid.UUID, body: ExtendBody) -> StayActionView:
    """Новый срок считает сервер: +N к дате выезда (просроченному — от сегодня),
    локальное время выезда сохраняется; доступ гостя следует автоматически."""
    context = await _authorized_receptionist(request)
    stay = await checkin.get_active_stay_or_404(stay_id)
    zone = await checkin.hotel_zone(context)
    new_checkout = checkin.extended_checkout(stay.check_out_at, zone, body.nights, now=utc_now())
    updated = await guests_api.extend_stay(stay_id, new_checkout)
    logger.info(
        "staff.stay_extended",
        stay_id=str(stay_id),
        nights=body.nights,
        user_id=str(context.user_id),
    )
    return StayActionView(
        room_number=updated.room_number,
        check_out_local=checkin.format_local(updated.check_out_at, zone),
    )


@router.post("/stays/{stay_id}/checkout", summary="Выселить гостя (доступ гаснет)")
async def check_out_stay(request: Request, stay_id: uuid.UUID) -> StayActionView:
    context = await _authorized_receptionist(request)
    stay = await guests_api.check_out(stay_id)
    logger.info("staff.stay_checked_out", stay_id=str(stay_id), user_id=str(context.user_id))
    zone = await checkin.hotel_zone(context)
    return StayActionView(
        room_number=stay.room_number,
        check_out_local=checkin.format_local(stay.check_out_at, zone),
    )


@router.get("/stays/{stay_id}/bindings", summary="Счётчик привязок Stay (поллинг 3 с)")
async def stay_bindings(request: Request, stay_id: uuid.UUID) -> BindingsCountView:
    """GET ничего не меняет — CSRF-щит не нужен (контракт router.py), роль
    проверяется как у страницы."""
    await _reception_role(request)
    return BindingsCountView(count=await guests_api.count_stay_sessions(stay_id))


# ---------------------------------------------------------------------------
# Состав команды (spec 0033 §7, PR F): только роль `manager`.
# ---------------------------------------------------------------------------


class InviteBody(BaseModel):
    """«Пригласить»: имя (его увидит сам сотрудник) и роль из мини-матрицы §3.2."""

    invited_name: str = Field(min_length=1, max_length=255)
    role_key: StaffRole

    @field_validator("invited_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("invited name must not be blank")
        return stripped


class RoleBody(BaseModel):
    """«Сменить роль»: новая роль членства в этом отеле."""

    role_key: StaffRole


class InviteLinkView(BaseModel):
    """Свежая ссылка-приглашение: показывается ровно один раз (в БД — хэш)."""

    invite_url: str
    invited_name: str
    expires_in_hours: int


async def _authorized_manager(request: Request) -> staff_auth.StaffContext:
    _require_json_same_origin(request)
    return await _manager_role(request)


@router.post("/team/invites", summary="Пригласить сотрудника (одноразовая ссылка)")
async def create_team_invite(request: Request, body: InviteBody) -> InviteLinkView:
    """Ссылку менеджер передаёт сам (WhatsApp/лично) — email-порта нет
    (spec 0033 §3.4). Старое приглашение того же человека не гасится
    автоматически: увидев две строки в списке, менеджер отзывает лишнюю."""
    context = await _authorized_manager(request)
    grant = await staff_invites.create_invite(
        context.tenant_id, body.role_key, body.invited_name, invited_by=context.user_id
    )
    return InviteLinkView(
        invite_url=team.invite_url(grant.invite_token),
        invited_name=body.invited_name,
        expires_in_hours=get_settings().staff_invite_ttl_hours,
    )


@router.post("/team/invites/{invite_id}/revoke", summary="Отозвать приглашение")
async def revoke_team_invite(request: Request, invite_id: uuid.UUID) -> dict[str, bool]:
    context = await _authorized_manager(request)
    await staff_invites.revoke_invite(
        invite_id, tenant_id=context.tenant_id, actor_user_id=context.user_id
    )
    return {"ok": True}


@router.post("/team/members/{user_id}/role", summary="Сменить роль сотрудника")
async def change_team_member_role(
    request: Request, user_id: uuid.UUID, body: RoleBody
) -> dict[str, bool]:
    """Действует со следующего запроса сотрудника; свою роль менеджер не
    меняет (ERR-AUTH-011) — иначе последний менеджер запер бы себе отель."""
    context = await _authorized_manager(request)
    await staff_team.change_member_role(
        user_id, context.tenant_id, body.role_key, actor_user_id=context.user_id
    )
    return {"ok": True}


@router.post("/team/members/{user_id}/deactivate", summary="Отключить сотрудника")
async def deactivate_team_member(request: Request, user_id: uuid.UUID) -> dict[str, bool]:
    """Сессии гаснут немедленно, членства отзываются (spec 0033 §3.3).
    Реактивации в v1 нет — вернувшийся сотрудник заводится заново."""
    context = await _authorized_manager(request)
    await staff_team.deactivate_member(user_id, context.tenant_id, actor_user_id=context.user_id)
    return {"ok": True}
