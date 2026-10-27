"""JSON-действия кабинета персонала (spec 0033 §5–§7): очередь заявок
(взять / готово / отменить, PR D), карточка заселения (bind-ссылка,
перевыпуск кода, переселение, продление, выезд, счётчик привязок, PR E),
состав команды (пригласить, отозвать ссылку, сменить уровень и службы, отключить, PR F;
суперадмину — ссылка нового пароля и стирание учётки, spec 0044) и
справочник отеля (добавить / изменить / удалить факт, spec 0036 §7).

Действия зовёт ванильный JS страниц (`static/queue.js`, `static/checkin.js`,
`static/team.js`, `static/knowledge.js`); опрос невзятых заявок —
`static/signal.js` (spec 0039 §3).
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
«уже взята» и обновляет список. Каждое действие над заявкой передаёт домену
службы сотрудника (`allowed_category_ids`, spec 0045 §5): заявка чужой службы —
403 `ERR-REQUESTS-007`, проверка — под той же блокировкой строки, что переход.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_auth, staff_invites, staff_superadmin, staff_team
from hospitality.platform.config import TenantConfig, mutate_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_manual_request
from hospitality.shared.ratelimit import consume_rate_limit
from hospitality.staff_portal import checkin, knowledge, scope, signal, team, turnovers
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): превышен лимит выпуска
# bind-ссылок по (tenant, stay). Отказ CSRF-щита (ERR-AUTH-009) — `browser.py`.
ERR_BIND_LINK_RATE_LIMITED = "ERR-AUTH-010"

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Очередь: любой уровень, а граница службы — в домене (spec 0045 §5, `scope.py`).
_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)
# Заселение и карточки Stay — служба ресепшена любого уровня и менеджер (spec 0045 §2).
_reception_role: Final = staff_auth.require_role(StaffRole.MANAGER, or_reception=True)
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


class NewRequestBody(BaseModel):
    """Форма «Новая заявка» (spec 0035 §5): три поля, и все три обязательны.

    `details` форма не заполняет, а `is_urgent` не спрашивает (§10): сотрудник
    у телефона пишет одну фразу, а не структурированную заявку; признак
    срочности ставит ЧП-перехват модели (spec 0034), и настоящее ЧП принявший
    звонок несёт службе голосом, а не через форму.
    """

    room_number: str = Field(min_length=1, max_length=20)
    category_id: uuid.UUID
    summary: str = Field(min_length=1, max_length=500)

    @field_validator("room_number", "summary")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("field must not be blank")
        return stripped


async def _authorized_actor(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
    return await _any_role(request)


async def _scoped_actor(
    request: Request,
) -> tuple[staff_auth.StaffContext, frozenset[uuid.UUID] | None]:
    """Действие над заявкой: кто действует и в каких службах (None — во всех)."""
    context = await _authorized_actor(request)
    return context, await scope.load_allowed_category_ids(context)


@router.post("/requests/{request_id}/claim", summary="Взять заявку в работу")
async def claim_request(request: Request, request_id: uuid.UUID) -> requests_api.ServiceRequestRead:
    """`new → in_progress`, пишет «кто взял» (claimed_by, spec 0033 §5).

    Тела у действия нет (JS шлёт `{}` ради Content-Type из CSRF-контракта);
    конфликт двух взявших разрешает карта переходов — второй получает 409.
    """
    context, allowed = await _scoped_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.IN_PROGRESS,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=context.actor(),
        allowed_category_ids=allowed,
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
    context, allowed = await _scoped_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.DONE,
        resolution_note=body.note,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=context.actor(),
        allowed_category_ids=allowed,
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
    context, allowed = await _scoped_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.CANCELLED,
        resolution_note=body.note,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=context.actor(),
        allowed_category_ids=allowed,
    )
    logger.info(
        "staff.request_cancelled",
        request_id=str(request_id),
        user_id=str(context.user_id),
    )
    return updated


class TransferBody(BaseModel):
    """«Передать»: служба, в которую уходит заявка (spec 0042 §2.4)."""

    category_id: uuid.UUID


@router.post("/requests/{request_id}/requeue", summary="Вернуть заявку в очередь")
async def requeue_request(
    request: Request, request_id: uuid.UUID
) -> requests_api.ServiceRequestRead:
    """`in_progress → new`, «кто взял» снимается (spec 0042 §2.1).

    Чужую взятую заявку своей службы может вернуть любой её сотрудник
    (spec 0045 §3): горничную со смены снимают сразу, не дожидаясь старшего.
    Тела нет — `{}` ради Content-Type из CSRF-контракта.
    """
    context, allowed = await _scoped_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.NEW,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=context.actor(),
        allowed_category_ids=allowed,
    )
    logger.info(
        "staff.request_requeued",
        request_id=str(request_id),
        user_id=str(context.user_id),
    )
    return updated


@router.post("/requests/{request_id}/transfer", summary="Передать заявку в другую службу")
async def transfer_request(
    request: Request, request_id: uuid.UUID, body: TransferBody
) -> requests_api.ServiceRequestRead:
    """Сменить службу открытой заявки; взятая возвращается в очередь (spec 0042 §2.2).

    Закрытая заявка или та же служба — 409 `ERR-REQUESTS-005`, служба другого
    отеля — 404 `ERR-REQUESTS-001`, заявка не из служб сотрудника — 403
    `ERR-REQUESTS-007`: проверки — в домене, здесь личность и её службы.
    Передать можно в любую службу отеля (spec 0045 §3).
    """
    context, allowed = await _scoped_actor(request)
    updated = await requests_api.transfer_request(
        request_id, body.category_id, acting_user=context.actor(), allowed_category_ids=allowed
    )
    logger.info(
        "staff.request_transferred",
        request_id=str(request_id),
        user_id=str(context.user_id),
    )
    return updated


@router.get("/requests/signal", summary="Невзятые заявки и номера доски для сигнала (опрос 15 с)")
async def requests_signal(request: Request) -> signal.SignalView:
    """Сигнал кабинета (spec 0039 §3): заявки — только `new`, без текста гостя;
    номера доски выездов — по правам доски (spec 0046 §7). Канон —
    `GET …/stays/{id}/bindings`: GET ничего не меняет, CSRF-щит не нужен, роль
    проверяется как у страницы. Опросы не журналируются (§8): это шум раз в
    15 секунд с каждой открытой вкладки."""
    context = await _any_role(request)
    return await signal.build_signal(context)


@router.post("/requests", summary="Создать заявку вручную (форма кабинета)")
async def create_manual_request(
    request: Request, body: NewRequestBody
) -> requests_api.ServiceRequestRead:
    """Ручной приём заявки (spec 0035 §5): звонок на ресепшен, разговор в коридоре.

    Второй ветки создания заявки здесь нет (P-5): та же
    `requests_api.create_request`, что у AI-инструмента, — а значит тот же
    дневной номер `#N`, то же событие `request.created` и то же уведомление в
    чат службы по маршрутизации spec 0026. Отличается ровно одно поле —
    `origin=staff_manual`, из которого считается доля Exit-критерия Phase 1.

    Язык гостя неизвестен (`None` → умолчание тенанта): гостя на этом пути нет,
    и статусные уведомления ему не адресуются — их адресует `request_origins`
    (диалог-источник), которого у ручной заявки не существует. Номер заявки
    гость узнаёт от сотрудника, а не от бота.

    Роль — любая из трёх и служба — любая (`_authorized_actor`, docs/RBAC.md,
    spec 0045 §3): звонок принимает ресепшен, а горничную ловят в коридоре.
    """
    context = await _authorized_actor(request)
    created = await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=body.category_id,
            origin=requests_api.ServiceRequestOrigin.STAFF_MANUAL,
            summary=body.summary,
            room_number=body.room_number,
            guest_language=None,
            is_urgent=False,
        ),
        acting_user=context.actor(),
    )
    logger.info(
        "staff.request_created_manually",
        request_id=str(created.id),
        daily_number=created.daily_number,
        user_id=str(context.user_id),
    )
    record_staff_manual_request()
    return created


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
    """Свежая ссылка привязки: QR и URL показываются один раз (в БД — хэш)."""

    bind_url: str
    qr_svg: str


class ReissuedCodeView(BaseModel):
    """Новые код и QR — показываются один раз (в БД только хэши)."""

    access_code: str
    qr_svg: str


class StayActionView(BaseModel):
    """Итог действия над Stay для обновления карточки без перерисовки."""

    room_number: str
    check_out_local: str
    # Только у «Выселить»: номер ушёл на доску «Выезды и уборка» (spec 0046 §5),
    # карточка переводит туда. None — хаускипинга в кабинете нет.
    turnover_url: str | None = None


class BindingsCountView(BaseModel):
    """Счётчик привязок Stay — поллинг индикатора «гость подключился» (3 с)."""

    count: int


async def _authorized_receptionist(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
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


@router.post("/stays/{stay_id}/bind-link", summary="Выпустить QR ссылки привязки (до выезда)")
async def issue_stay_bind_link(request: Request, stay_id: uuid.UUID) -> BindLinkView:
    """Ещё одна ссылка того же Stay: уже напечатанные не гаснут (spec 0033 §6)."""
    context = await _authorized_receptionist(request)
    await _enforce_bind_link_issue_limit(context, stay_id)
    token = await guests_api.issue_bind_link(stay_id)
    url = checkin.bind_link_url(context.tenant_slug, token)
    logger.info("staff.bind_link_issued", stay_id=str(stay_id), user_id=str(context.user_id))
    return BindLinkView(bind_url=url, qr_svg=checkin.qr_svg(url))


@router.post("/stays/{stay_id}/reissue-code", summary="Перевыпустить код и QR")
async def reissue_stay_code(request: Request, stay_id: uuid.UUID) -> ReissuedCodeView:
    """Ответ на «гость потерял листок»: перевыпуск кода гасит старый код и все
    QR этого Stay (#354), поэтому новый QR выпускается следом — карточка
    показывает новые код и QR, и листок готов к печати одним нажатием. Лимит —
    тот же, что у выпуска QR: каждый перевыпуск рождает ссылку."""
    context = await _authorized_receptionist(request)
    await _enforce_bind_link_issue_limit(context, stay_id)
    code = await guests_api.reissue_access_code(stay_id, acting_user=context.actor())
    token = await guests_api.issue_bind_link(stay_id)
    logger.info("staff.stay_code_reissued", stay_id=str(stay_id), user_id=str(context.user_id))
    return ReissuedCodeView(
        access_code=guests_api.format_access_code(code),
        qr_svg=checkin.qr_svg(checkin.bind_link_url(context.tenant_slug, token)),
    )


@router.post("/stays/{stay_id}/move", summary="Переселить гостя в другую комнату")
async def move_stay(request: Request, stay_id: uuid.UUID, body: MoveBody) -> StayActionView:
    context = await _authorized_receptionist(request)
    stay = await guests_api.move_stay(stay_id, body.room_number, acting_user=context.actor())
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
    updated = await guests_api.extend_stay(
        stay_id, new_checkout, nights=body.nights, acting_user=context.actor()
    )
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
    """Выезд гостя; номер заодно уходит хаускипингу на доску (spec 0046 §5)."""
    context = await _authorized_receptionist(request)
    stay = await guests_api.check_out(stay_id, acting_user=context.actor())
    logger.info("staff.stay_checked_out", stay_id=str(stay_id), user_id=str(context.user_id))
    turnover_url = await turnovers.report_after_stay_checkout(context, stay.room_number)
    zone = await checkin.hotel_zone(context)
    return StayActionView(
        room_number=stay.room_number,
        check_out_local=checkin.format_local(stay.check_out_at, zone),
        turnover_url=turnover_url,
    )


@router.get("/stays/{stay_id}/bindings", summary="Счётчик привязок Stay (поллинг 3 с)")
async def stay_bindings(request: Request, stay_id: uuid.UUID) -> BindingsCountView:
    """GET ничего не меняет — CSRF-щит не нужен (контракт router.py), роль
    проверяется как у страницы."""
    await _reception_role(request)
    return BindingsCountView(count=await guests_api.count_stay_sessions(stay_id))


# ---------------------------------------------------------------------------
# Состав команды (spec 0033 §7, PR F): только уровень `manager`.
# ---------------------------------------------------------------------------


class InviteBody(BaseModel):
    """«Пригласить»: имя (его увидит сам сотрудник), логин, уровень и службы.

    Формат и свободу логина проверяет `staff_invites.create_invite`
    (ERR-AUTH-012, spec 0037 §4), службы — `team.normalized_service_keys`
    (ERR-AUTH-015, spec 0045 §6); здесь только потолки размера тела."""

    invited_name: str = Field(min_length=1, max_length=255)
    login: str = Field(max_length=64)
    role_key: StaffRole
    service_keys: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("invited_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("invited name must not be blank")
        return stripped


class AccessBody(BaseModel):
    """«Изменить»: уровень и службы членства в этом отеле — одной формой (spec 0045 §6)."""

    role_key: StaffRole
    service_keys: list[str] = Field(default_factory=list, max_length=50)


class InviteLinkView(BaseModel):
    """Свежая ссылка-приглашение: показывается ровно один раз (в БД — хэш).
    `login` — нормализованный логин сотрудника, как он будет входить."""

    invite_url: str
    invited_name: str
    login: str
    expires_in_hours: int


async def _authorized_manager(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
    return await _manager_role(request)


@router.post("/team/invites", summary="Пригласить сотрудника (одноразовая ссылка)")
async def create_team_invite(request: Request, body: InviteBody) -> InviteLinkView:
    """Ссылку менеджер передаёт сам (WhatsApp/лично) — email-порта нет
    (spec 0033 §3.4). Старое приглашение того же человека не гасится
    автоматически: увидев две строки в списке, менеджер отзывает лишнюю."""
    context = await _authorized_manager(request)
    service_keys = await team.normalized_service_keys(body.role_key, body.service_keys)
    grant = await staff_invites.create_invite(
        context.tenant_id,
        body.role_key,
        body.invited_name,
        body.login,
        invited_by=context.user_id,
        service_keys=service_keys,
    )
    return InviteLinkView(
        invite_url=team.invite_url(grant.invite_token),
        invited_name=body.invited_name,
        login=grant.login,
        expires_in_hours=get_settings().staff_invite_ttl_hours,
    )


@router.post("/team/invites/{invite_id}/revoke", summary="Отозвать приглашение")
async def revoke_team_invite(request: Request, invite_id: uuid.UUID) -> dict[str, bool]:
    context = await _authorized_manager(request)
    await staff_invites.revoke_invite(
        invite_id, tenant_id=context.tenant_id, actor_user_id=context.user_id
    )
    return {"ok": True}


@router.post("/team/members/{user_id}/access", summary="Сменить уровень и службы сотрудника")
async def change_team_member_access(
    request: Request, user_id: uuid.UUID, body: AccessBody
) -> dict[str, bool]:
    """Действует со следующего запроса сотрудника; свой уровень менеджер не
    меняет (ERR-AUTH-011) — иначе последний менеджер запер бы себе отель."""
    context = await _authorized_manager(request)
    service_keys = await team.normalized_service_keys(body.role_key, body.service_keys)
    await staff_team.change_member_access(
        user_id,
        context.tenant_id,
        body.role_key,
        service_keys=service_keys,
        actor_user_id=context.user_id,
    )
    return {"ok": True}


@router.post("/team/members/{user_id}/deactivate", summary="Отключить сотрудника")
async def deactivate_team_member(request: Request, user_id: uuid.UUID) -> dict[str, bool]:
    """Сессии гаснут немедленно, членства отзываются (spec 0033 §3.3).
    Реактивации в v1 нет — вернувшийся сотрудник заводится заново."""
    context = await _authorized_manager(request)
    await staff_team.deactivate_member(user_id, context.tenant_id, actor_user_id=context.user_id)
    return {"ok": True}


class PasswordResetLinkView(BaseModel):
    """Свежая ссылка нового пароля: показывается ровно один раз (в БД — хэш)."""

    reset_url: str
    login: str
    expires_in_hours: int


async def _authorized_superadmin(request: Request) -> staff_auth.StaffContext:
    require_json_same_origin(request)
    return await staff_superadmin.require_superadmin(request)


@router.post(
    "/team/members/{user_id}/password-reset",
    summary="Суперадмин: ссылка нового пароля сотруднику (spec 0044 §4)",
)
async def reset_team_member_password(request: Request, user_id: uuid.UUID) -> PasswordResetLinkView:
    """Только суперадмин (не он — 403 ERR-AUTH-003). До использования ссылки
    ничего не меняется: старый пароль работает, сессии живы."""
    context = await _authorized_superadmin(request)
    grant = await staff_superadmin.issue_password_reset(
        user_id, context.tenant_id, actor_user_id=context.user_id
    )
    return PasswordResetLinkView(
        reset_url=team.reset_url(grant.reset_token),
        login=grant.login,
        expires_in_hours=get_settings().staff_password_reset_ttl_hours,
    )


@router.post("/team/members/{user_id}/erase", summary="Суперадмин: стереть учётку (spec 0044 §5)")
async def erase_team_member(request: Request, user_id: uuid.UUID) -> dict[str, bool]:
    """Только суперадмин. Отключение — в том же шаге; восстановления нет."""
    context = await _authorized_superadmin(request)
    await staff_superadmin.erase_member(user_id, context.tenant_id, actor_user_id=context.user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Справочник отеля (spec 0036 §7, issue #335): только роль `manager`.
# Все три действия — POST с темой В ТЕЛЕ: тема — свободный текст, а «/» в пути
# даёт 404 даже процентно-кодированным, и такой факт стал бы нередактируемым.
# ---------------------------------------------------------------------------


class HotelFactBody(BaseModel):
    """Факт из формы. Пределов схема НЕ несёт намеренно (§7): их проверяет
    `knowledge` под блокировкой и отвечает надписью §8.1, а предел здесь дал бы
    422 `ERR-PLATFORM-002` — один рамочный текст на десять причин."""

    topic: str
    answer: str
    valid_until: date | None = None


class HotelFactUpdateBody(HotelFactBody):
    """`topic` — какой факт правится, `new_topic` — новая тема (переименование)."""

    new_topic: str | None = None


class HotelFactDeleteBody(BaseModel):
    topic: str


async def _edit_hotel_facts(
    request: Request, mutate: Callable[[TenantConfig], TenantConfig], event: str, topic: str
) -> dict[str, bool]:
    context = await _authorized_manager(request)
    async with platform_session_scope() as session:
        await mutate_tenant_config(session, context.tenant_id, mutate)
    # Тема — не ПД (данные отеля, spec 0036 §9); actor обязателен (spec 0033 §9).
    logger.info(event, topic=topic, user_id=str(context.user_id))
    return {"ok": True}


@router.post("/knowledge/facts", summary="Справочник: добавить факт")
async def add_hotel_fact(request: Request, body: HotelFactBody) -> dict[str, bool]:
    return await _edit_hotel_facts(
        request,
        knowledge.adding(body.topic, body.answer, body.valid_until),
        "staff.hotel_fact_created",
        body.topic.strip(),
    )


@router.post("/knowledge/facts/update", summary="Справочник: изменить факт")
async def update_hotel_fact(request: Request, body: HotelFactUpdateBody) -> dict[str, bool]:
    return await _edit_hotel_facts(
        request,
        knowledge.updating(body.topic, body.new_topic, body.answer, body.valid_until),
        "staff.hotel_fact_updated",
        (body.new_topic or body.topic).strip(),
    )


@router.post("/knowledge/facts/delete", summary="Справочник: удалить факт")
async def delete_hotel_fact(request: Request, body: HotelFactDeleteBody) -> dict[str, bool]:
    return await _edit_hotel_facts(
        request, knowledge.deleting(body.topic), "staff.hotel_fact_deleted", body.topic.strip()
    )
