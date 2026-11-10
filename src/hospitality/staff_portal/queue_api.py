"""JSON-действия очереди заявок (spec 0033 §5, PR D): взять / готово / отменить,
вернуть в очередь и передать в другую службу (spec 0042), создать вручную
(spec 0035 §5) и опрос сигнала кабинета (spec 0039 §3).

Канон JSON-действия кабинета (README пакета, «Типовые сценарии»): действия
карточки Stay (`stay_api.py`), состава команды (`team_api.py`), справочника
(`knowledge_api.py`) и остальных разделов копируют этот файл.

Действия зовёт ванильный JS страниц (`static/queue.js`, `static/new_request.js`);
опрос невзятых заявок — `static/signal.js`.
Авторизация — `staff_auth.require_role` НАПРЯМУЮ (JSON-исходы: 401/403 уходят
каноническим конвертом ошибок R-8, без браузерных редиректов `pages.page_context`);
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
from typing import Final

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from hospitality.modules.requests import api as requests_api
from hospitality.platform import staff_auth
from hospitality.platform.models import StaffRole
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_staff_manual_request
from hospitality.staff_portal import floors, scope, signal
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api", tags=["staff-portal"])

# Очередь: любой уровень, а граница службы — в домене (spec 0045 §5, `scope.py`).
_any_role: Final = staff_auth.require_role(StaffRole.STAFF, StaffRole.SENIOR, StaffRole.MANAGER)


class CompleteBody(BaseModel):
    """«Готово»: примечание опционально (частичное выполнение, spec 0021/0030).

    `remainder` — галочка «доделать остальное позже» (spec 0079 §2.3): остаток —
    новой заявкой в очереди службы. Только с примечанием — им остаток и описан.
    """

    note: str | None = Field(default=None, max_length=500)
    remainder: bool = False

    @field_validator("note")
    @classmethod
    def _strip_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _remainder_needs_note(self) -> CompleteBody:
        if self.remainder and self.note is None:
            raise ValueError("remainder requires a note")
        return self


class CompletedRequest(requests_api.ServiceRequestRead):
    """Ответ «Готово»: закрытая заявка и `#N` остатка, если он заведён (spec 0079 §2.3)."""

    remainder_daily_number: int | None = None


class CancelBody(BaseModel):
    """«Отменить»: причина из списка обязательна (spec 0057 §2).

    Текст — только у «Другое», и там он обязателен: его прочитает гость. У прочих
    причин гостю уходит свой текст, присланная заметка отбрасывается. «Гость
    отменил сам» ставит домен на отмене гостем — персоналу её не выбрать.
    """

    reason: requests_api.CancelReason
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _note_only_for_other(self) -> CancelBody:
        if self.reason is requests_api.CancelReason.BY_GUEST:
            raise ValueError("by_guest is set by the domain, not by staff")
        if self.reason is not requests_api.CancelReason.OTHER:
            self.note = None
            return self
        self.note = (self.note or "").strip() or None
        if self.note is None:
            raise ValueError("cancellation note is required for reason 'other'")
        return self


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
) -> CompletedRequest:
    context, allowed = await _scoped_actor(request)
    remainder_daily_number: int | None = None
    if body.remainder and body.note is not None:
        outcome = await requests_api.complete_with_remainder(
            request_id,
            resolution_note=body.note,
            initiator=requests_api.RequestInitiator.STAFF,
            acting_user=context.actor(),
            allowed_category_ids=allowed,
        )
        updated = outcome.closed
        remainder_daily_number = outcome.remainder.daily_number
    else:
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
        with_remainder=body.remainder,
    )
    return CompletedRequest.model_validate(
        updated.model_dump() | {"remainder_daily_number": remainder_daily_number}
    )


@router.post("/requests/{request_id}/cancel", summary="Отменить заявку (причина обязательна)")
async def cancel_request(
    request: Request, request_id: uuid.UUID, body: CancelBody
) -> requests_api.ServiceRequestRead:
    context, allowed = await _scoped_actor(request)
    updated = await requests_api.change_request_status(
        request_id,
        requests_api.RequestStatus.CANCELLED,
        resolution_note=body.note,
        cancel_reason=body.reason,
        initiator=requests_api.RequestInitiator.STAFF,
        acting_user=context.actor(),
        allowed_category_ids=allowed,
    )
    logger.info(
        "staff.request_cancelled",
        request_id=str(request_id),
        user_id=str(context.user_id),
        reason=body.reason.value,
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
    15 секунд с каждой открытой вкладки. Хаускипингу — с раздачей этажей
    (spec 0053 §4). Запертую короткую сессию опрос пропускает и отдаёт её
    состояние в `session` (spec 0073 §2)."""
    context = await _any_role(request)
    view = await signal.build_signal(context, await floors.routing_for_staff(context))
    view.session = signal.session_state(context)
    return view


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
