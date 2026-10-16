"""Эскалация заводит заявку (spec 0039 §2, issue #292) — шаги 1–2 из §2.6.

У отеля с заданной службой эскалаций (`TenantConfig.escalation_category_key`)
каждая эскалация — ЧП, «ИИ не ответил», «бот не смог оформить» — становится
заявкой в этой службе с `origin=escalation`. Так она получает всё, что у заявки
уже есть: «Взять» с именем взявшего, «Готово», «Отменить» с причиной,
просрочку, бейдж «🚨 срочно», место в очереди кабинета и в сводке дня. У отеля
без бота это единственный путь, по которому персонал вообще узнаёт о госте,
которому нужен человек: подписчик Telegram без чата молчит (`ERR-TELEGRAM-002`).

Порядок — §2.6: сначала заявка (своя транзакция, best-effort), затем строка
эскалации со ссылкой на неё и outbox (`publish_escalation`), затем — уже в ходе
гостя — ответ гостю. Шаг заявки не мешает эскалации НИКАКИМ исключением:
spec 0034 обещает пути ЧП независимость от посторонних сбоев, и у отеля с
Telegram ЧП-сообщение обязано уйти в чат, даже если код заявок упал.

Привязки к диалогу (`request_origins`, ADR-011) у такой заявки нет намеренно
(§2.7): гостю не придёт «ваша заявка выполнена» в ответ на ЧП, модель не увидит
её среди активных заявок диалога, гость не отменит её через бота.

Заявка заводится прямо в ходе гостя, а не подписчиком outbox: очередь воркера
на путь «гость → кабинет» не влияет (§2.6).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from hospitality.ai.escalation import EscalationContext, EscalationReason
from hospitality.channels.common.events import list_escalation_request_ids, publish_escalation
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_escalation_request
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)


@dataclass(frozen=True)
class _CardTemplate:
    """Как эскалация с этой причиной выглядит заявкой (spec 0039 §2.2)."""

    summary: str
    is_urgent: bool


# Таблица §2.2 — единственное место шаблонов. Причина без строки здесь — ошибка
# теста (`test_every_escalation_reason_has_a_card_template`), а не молчаливый
# пропуск; в проде её ловит общий перехват шага и пишет `cause=unexpected`.
# `{text}` — дословная реплика гостя без перевода: уведомления переводят только
# `origin=guest_chat` (spec 0035 §5), а у `llm_unavailable` модели и нет.
_CARD_TEMPLATES: Final[dict[EscalationReason, _CardTemplate]] = {
    EscalationReason.EMERGENCY: _CardTemplate("ЧП — сообщение гостя: «{text}»", is_urgent=True),
    EscalationReason.LLM_UNAVAILABLE: _CardTemplate(
        "Бот не смог ответить, гость ждёт сотрудника: «{text}»", is_urgent=False
    ),
    EscalationReason.UNKNOWN_TOOL: _CardTemplate(
        "Бот не смог оформить просьбу, гость ждёт сотрудника: «{text}»", is_urgent=False
    ),
    EscalationReason.TOOL_EXECUTION_FAILED: _CardTemplate(
        "Бот не смог оформить просьбу, гость ждёт сотрудника: «{text}»", is_urgent=False
    ),
    # spec 0040 (#101): модель позвала сотрудника инструментом. Суть просьбы —
    # в подробностях («Что бот понял»): на ходе «да» реплика гостя — только «да».
    EscalationReason.GUEST_REQUESTED_STAFF: _CardTemplate(
        "Гость просит сотрудника: «{text}»", is_urgent=False
    ),
}
_DETAILS_TEMPLATE: Final = "Что бот понял: {action_summary}"

# Пределы полей `ServiceRequestCreate` (summary / details / room_number). Схема —
# граница модуля requests и отвергла бы длинное значение целиком; здесь текст
# гостя обрезается с «…», чтобы длинное сообщение не стоило карточки. Сверку
# со схемой держит тест `test_card_limits_match_request_schema`.
_SUMMARY_MAX_CHARS: Final = 500
_DETAILS_MAX_CHARS: Final = 4000
_ROOM_MAX_CHARS: Final = 20


async def escalate(
    conversation_id: uuid.UUID,
    inbound_message_id: uuid.UUID,
    *,
    chat_id: str,
    guest_message: str,
    escalation: EscalationContext,
    verified_room_number: str | None,
) -> None:
    """Позвать человека: заявка (если отель задал службу) → факт эскалации в outbox.

    Вызывается ДО реплики гостю (инвариант spec 0022). Шаг заявки исключений
    наружу не пропускает; исключение `publish_escalation` — пропускает, как
    раньше: гость тогда не получит ответа, но карточка в кабинете уже есть.
    """
    request_id = await _open_escalation_request(
        conversation_id,
        guest_message=guest_message,
        escalation=escalation,
        verified_room_number=verified_room_number,
    )
    await publish_escalation(
        conversation_id,
        inbound_message_id,
        chat_id=chat_id,
        guest_message=guest_message,
        escalation=escalation,
        request_id=request_id,
    )


async def _open_escalation_request(
    conversation_id: uuid.UUID,
    *,
    guest_message: str,
    escalation: EscalationContext,
    verified_room_number: str | None,
) -> uuid.UUID | None:
    """Шаг 1 §2.6: завести заявку; None — не заведена (причина — в журнале).

    Перехватывается ЛЮБОЕ исключение, а не только `AppError`: `create_request`
    штатно пробрасывает `IntegrityError`, а ещё есть собственный код шага
    (конфиг, поиск категории, дедупликация, шаблоны). Текст исключения в журнал
    не идёт — только имя класса: у движка БД не включён `hide_parameters` (#346),
    и текст `IntegrityError` несёт параметры SQL, то есть `summary` с текстом
    гостя (§8). Уровень — WARNING, а не ERROR: ERROR-лог становится событием
    Sentry, и SDK прикладывает к нему тело запроса гостя (#433). Сбой виден
    метрикой `escalation_requests_total{outcome="failed"}`.
    """
    reason = escalation.reason.value
    try:
        return await _try_open(
            conversation_id,
            guest_message=guest_message,
            escalation=escalation,
            verified_room_number=verified_room_number,
        )
    except AppError as error:
        logger.warning(
            "escalation_request_skipped",
            cause=error.code,
            reason=reason,
            conversation_id=str(conversation_id),
        )
    except Exception as error:  # best-effort: путь эскалации не зависит от кода заявок (§2.6)
        logger.warning(
            "escalation_request_skipped",
            cause="unexpected",
            error_type=type(error).__name__,
            reason=reason,
            conversation_id=str(conversation_id),
        )
    record_escalation_request("failed")
    return None


async def _try_open(
    conversation_id: uuid.UUID,
    *,
    guest_message: str,
    escalation: EscalationContext,
    verified_room_number: str | None,
) -> uuid.UUID | None:
    reason = escalation.reason.value
    category_key = await _escalation_category_key()
    if category_key is None:
        # Поведение до spec 0039 (§2.3): только эскалация. Норма для отелей,
        # которым хватает Telegram, и для staging.
        logger.info(
            "escalation_request_skipped",
            cause="no_category_key",
            reason=reason,
            conversation_id=str(conversation_id),
        )
        record_escalation_request("disabled")
        return None

    template = _CARD_TEMPLATES[escalation.reason]
    unclaimed_id = (
        None if template.is_urgent else await _unclaimed_escalation_request(conversation_id)
    )
    if unclaimed_id is not None:
        logger.info(
            "escalation_request_deduplicated",
            request_id=str(unclaimed_id),
            reason=reason,
            conversation_id=str(conversation_id),
        )
        record_escalation_request("deduplicated")
        return None

    category_id = await _category_id(category_key)
    if category_id is None:
        # Ключ задан, а службы у отеля нет (удалили руками): обращение не
        # падает, гость получает прежний ответ (§2.3). Онбординг такой ключ не
        # записывает, поэтому это дрейф данных, а не настройка.
        logger.warning(
            "escalation_request_skipped",
            cause="category_missing",
            error_code=requests_api.ERR_REQUESTS_CATEGORY_NOT_FOUND,
            reason=reason,
            conversation_id=str(conversation_id),
        )
        record_escalation_request("failed")
        return None

    created = await requests_api.create_request(
        _card(category_id, template, guest_message, escalation, verified_room_number)
    )
    logger.info(
        "escalation_request_created",
        request_id=str(created.id),
        reason=reason,
        is_urgent=created.is_urgent,
        conversation_id=str(conversation_id),
    )
    record_escalation_request("created")
    return created.id


def _card(
    category_id: uuid.UUID,
    template: _CardTemplate,
    guest_message: str,
    escalation: EscalationContext,
    verified_room_number: str | None,
) -> requests_api.ServiceRequestCreate:
    """Заявка по таблице §2.2: суть — шаблон с репликой, подробности — что понял бот."""
    action_summary = (escalation.action_summary or "").strip()
    return requests_api.ServiceRequestCreate(
        category_id=category_id,
        origin=requests_api.ServiceRequestOrigin.ESCALATION,
        summary=_fill(template.summary, guest_message.strip(), _SUMMARY_MAX_CHARS),
        details=(
            _fill(_DETAILS_TEMPLATE, action_summary, _DETAILS_MAX_CHARS, key="action_summary")
            if action_summary
            else None
        ),
        room_number=_room(verified_room_number, escalation.room_number),
        is_urgent=template.is_urgent,
    )


async def _category_id(category_key: str) -> uuid.UUID | None:
    """Id категории тенанта по ключу; None — такой службы у отеля нет."""
    for category in await requests_api.list_categories():
        if category.key == category_key:
            return category.id
    return None


async def _escalation_category_key() -> str | None:
    """Служба эскалаций тенанта; тенант без конфига — её нет (не ошибка).

    Незавершённый онбординг (служебный smoke-тенант, демо) по определению не
    задал службу — это `no_category_key`, а не сбой. Прочие отказы конфига
    (ERR-PLATFORM-006 — конфиг не проходит схему) — наружу, в `failed`.
    """
    try:
        async with session_scope() as session:
            config = await load_tenant_config(session, current_tenant_id())
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        return None
    return config.escalation_category_key


async def _unclaimed_escalation_request(conversation_id: uuid.UUID) -> uuid.UUID | None:
    """Невзятая (`new`) эскалационная заявка диалога — срочная или нет (§2.5).

    Держит только `new`: такая карточка и так видна в очереди. Взятую могли не
    закрыть — у неё нет ни срока, ни напоминания (#120), и одна забытая
    «Готово» молча съела бы все следующие эскалации этого диалога.

    Два сообщения одного гостя, обработанные одновременно, могут оба не
    увидеть друг друга и дать две карточки: замок на диалог ради этого не
    заводится — лишняя карточка закрывается в два касания, а замок на пути ЧП
    был бы новой точкой отказа.
    """
    request_ids = await list_escalation_request_ids(conversation_id)
    if not request_ids:
        return None
    for request in await requests_api.list_open_requests_by_ids(request_ids):
        if request.status is requests_api.RequestStatus.NEW:
            return request.id
    return None


def _fill(template: str, value: str, limit: int, *, key: str = "text") -> str:
    """Подставить значение в шаблон, обрезав его с «…», чтобы итог влез в `limit`."""
    free = limit - len(template.format(**{key: ""}))
    if len(value) > free:
        value = value[: free - 1] + "…"
    return template.format(**{key: value})


def _room(verified: str | None, from_context: str | None) -> str | None:
    """Комната карточки: из привязки канала, иначе из контекста эскалации (§2.2).

    Контекст несёт сырой аргумент несостоявшегося вызова инструмента — он мог и
    не пройти схему (ради этого вызов и упал). Комната длиннее поля заявки — не
    комната: лучше карточка без комнаты, чем карточки нет вовсе.
    """
    for candidate in (verified, from_context):
        if candidate and len(candidate) <= _ROOM_MAX_CHARS:
            return candidate
    return None
