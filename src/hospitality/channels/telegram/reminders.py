"""Напоминание о невзятой заявке (issue #57, spec 0028).

Уведомление о новой заявке уходит в чат службы один раз. Если его в тот момент
никто не прочитал (смена, всплеск, ночь), заявка исчезает из поля зрения
навсегда: понятия «просрочено» в системе нет. Для гостя это выглядит как «отель
принял заказ и забыл».

Поэтому воркер раз в `worker_reminder_interval_seconds` зовёт
`remind_unclaimed_requests`: она ищет заявки в статусе `new` старше срока
тенанта и присылает в чат ИХ службы повторное сообщение с кнопками. Механика —
канон периодической задачи `cleanup_processed_events` (ADR-009): ни планировщика,
ни отдельной джобы (NG-8).

Границы намеренно узкие (issue #57 задаёт этот порядок):

- **одно напоминание на заявку** — ключ идемпотентности `staff:request_unclaimed:<id>`
  на исходящем `Message` (P-8, тот же механизм, что у остальных уведомлений);
  лестница «через 15 → через 30 → менеджеру» — это SLA-движок, а «менеджеру»
  без сущности `User` (ADR-008) адресовать некому;
- **только `new`** — «никто не взял»; заявка `in_progress`, висящая сутками, —
  вопрос к владельцу, которого в Phase 0 нет (ADR-013);
- **без LLM** — фоновая задача не должна ни стоить токенов, ни зависеть от
  доступности модели (канон `notify_staff_on_request_cancelled_by_guest`).

Мультитенантность (P-4): список тенантов берётся платформенной сессией, вся
работа с заявками — внутри `tenant_context` каждого. Кросс-тенантного запроса к
бизнес-таблице здесь нет и быть не может.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Final

import structlog

from hospitality.channels.common.store import (
    ensure_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.telegram import keyboards
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.channels.telegram.routing import (
    ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED,
    category_name,
    current_correlation_id,
    log_routing,
)
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import TenantConfig, list_configured_tenant_ids, load_tenant_config
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8). Два, а не один: у сбоя
# одной отправки и у падения прогона целиком разный диагноз и разное действие
# дежурного (Bot API против инфраструктуры) — та же логика, что у пары
# ERR-EVENTS-003/004.
ERR_TELEGRAM_REMINDER_NOT_SENT = "ERR-TELEGRAM-004"
ERR_TELEGRAM_REMINDER_SCAN_FAILED = "ERR-TELEGRAM-005"

# Страховка от неограниченного скана, а не настройка: крутить её незачем, а
# видеть упирание надо (WARNING ниже). На пилотных объёмах (десятки заявок в
# сутки) предел недостижим.
_SCAN_LIMIT: Final = 200

# Ключ идемпотентности напоминания (P-8). Третий сегмент — id заявки: по нему
# `load_request_id_for_staff_message` резолвит реплай-команду на напоминание.
IDEMPOTENCY_KEY_PREFIX: Final = "staff:request_unclaimed"


async def remind_unclaimed_requests(*, sender: TelegramSender, default_staff_chat_id: str) -> int:
    """Один прогон: напомнить о невзятых заявках всех тенантов. Вернуть их число.

    Зовётся из цикла воркера (`hospitality/worker.py`) и тестами напрямую.
    Прогон получает собственный `correlation_id` (§10.2): входящего запроса у
    фоновой задачи нет, а связать логи одного прогона и записанные им сообщения
    надо. Сбой на одном тенанте не отменяет остальных.
    """
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        async with platform_session_scope() as session:
            tenant_ids = await list_configured_tenant_ids(session)

        candidates = 0
        reminded = 0
        for tenant_id in tenant_ids:
            try:
                tenant_candidates, tenant_reminded = await _remind_tenant(
                    tenant_id, sender=sender, default_staff_chat_id=default_staff_chat_id
                )
            except Exception:
                # Сбой на одном отеле (битые данные, недоступная в этот момент
                # БД) не отменяет напоминания у остальных: скан обязан обойти
                # всех, а не остановиться на первом же.
                logger.error(
                    "unclaimed_request_scan_failed",
                    error_code=ERR_TELEGRAM_REMINDER_SCAN_FAILED,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
                continue
            candidates += tenant_candidates
            reminded += tenant_reminded
        # `candidates` отличает «просроченных заявок не было» от «были, но все
        # уже напомнены/отфильтрованы» — без него тишина неинтерпретируема.
        logger.info(
            "unclaimed_requests_scanned",
            tenants=len(tenant_ids),
            candidates=candidates,
            reminded=reminded,
        )
        return reminded


async def _remind_tenant(
    tenant_id: uuid.UUID, *, sender: TelegramSender, default_staff_chat_id: str
) -> tuple[int, int]:
    """Напомнить о невзятых заявках одного тенанта.

    Возвращает (сколько заявок попало в срез, сколько напоминаний ушло).
    """
    # tenant_id — явно: до входа в tenant_context его в лог-контексте нет, а без
    # него непонятно, у какого отеля не читается конфиг (§10.1).
    with structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)):
        try:
            async with platform_session_scope() as session:
                config = await load_tenant_config(session, tenant_id)
        except AppError as error:
            # Дрейф схемы конфига или гонка с удалением тенанта: пропускаем его,
            # остальные отели скан обязан обойти.
            logger.warning("unclaimed_request_scan_config_unavailable", error_code=error.code)
            return 0, 0

    delay = config.min_reminder_delay()
    if delay is None:
        return 0, 0  # напоминания у этого отеля выключены — заявки не читаем вовсе

    with tenant_context(tenant_id):
        candidates = await requests_api.list_unclaimed_requests(
            created_before=utc_now() - delay, limit=_SCAN_LIMIT
        )
        if len(candidates) == _SCAN_LIMIT:
            # Скрытая усечённая выборка выглядела бы как «всё в порядке».
            logger.warning("unclaimed_request_scan_truncated", limit=_SCAN_LIMIT)
        categories = {category.id: category for category in await requests_api.list_categories()}

        reminded = 0
        now = utc_now()
        for request in candidates:
            category = categories.get(request.category_id)
            request_delay = config.reminder_delay_for(category.key if category else None)
            if request_delay is None or request.created_at + request_delay > now:
                continue  # в срез попала по чужому, более короткому сроку
            if await _remind_one(
                request,
                category=category,
                config=config,
                now=now,
                sender=sender,
                default_staff_chat_id=default_staff_chat_id,
            ):
                reminded += 1
        return len(candidates), reminded


async def _remind_one(
    request: requests_api.ServiceRequestRead,
    *,
    category: requests_api.RequestCategoryRead | None,
    config: TenantConfig,
    now: datetime,
    sender: TelegramSender,
    default_staff_chat_id: str,
) -> bool:
    """Напомнить об одной заявке; вернуть, ушло ли сообщение.

    Адресат — то же правило, что у остальных staff-сообщений
    (`TenantConfig.staff_chat_for`, spec 0026): чат категории → дефолтный.
    Конфиг здесь уже прочитан прогоном, поэтому берётся из него напрямую —
    подписчикам-уведомлениям его перечитывает `routing.staff_chat_for_category`,
    у которой нет прогона на руках.

    Сбой отправки не пробрасывается: он не должен отменять напоминания по
    остальным заявкам. Ретрай не нужен конструктивно — ключ идемпотентности
    пишется только после успешной отправки, поэтому следующий прогон повторит
    попытку сам (P-8).
    """
    staff_chat_id = config.staff_chat_for(
        category.key if category is not None else None, default=default_staff_chat_id
    )
    if not staff_chat_id:
        logger.warning(
            "telegram_staff_chat_not_configured",
            error_code=ERR_TELEGRAM_STAFF_CHAT_NOT_CONFIGURED,
            request_id=str(request.id),
        )
        return False

    idempotency_key = f"{IDEMPOTENCY_KEY_PREFIX}:{request.id}"
    if await notification_already_sent(idempotency_key):
        return False  # уже напоминали: одно напоминание на заявку

    age = now - request.created_at
    text = _reminder_text(request, category, age)
    try:
        conversation_id = await ensure_conversation(CHANNEL, staff_chat_id)
        sent_id = await sender.send_message(
            staff_chat_id,
            text,
            reply_markup=keyboards.keyboard_for_status(request.id, request.status),
        )
        # Запись — только после успешной отправки (не «соврать» историей) и она
        # же гасит повтор: гонку двух воркеров закрывает уникальное ограничение
        # (tenant_id, idempotency_key).
        await record_outbound_message(
            conversation_id,
            text,
            current_correlation_id(),
            external_message_id=sent_id,
            idempotency_key=idempotency_key,
        )
    except Exception:
        logger.error(
            "unclaimed_request_reminder_failed",
            error_code=ERR_TELEGRAM_REMINDER_NOT_SENT,
            request_id=str(request.id),
            exc_info=True,
        )
        return False

    logger.info(
        "unclaimed_request_reminded",
        request_id=str(request.id),
        daily_number=request.daily_number,
        age_minutes=int(age.total_seconds() // 60),
        category_key=category.key if category is not None else None,
        chat_id=staff_chat_id,
    )
    log_routing(staff_chat_id, category, default_staff_chat_id)
    return True


def _reminder_text(
    request: requests_api.ServiceRequestRead,
    category: requests_api.RequestCategoryRead | None,
    age: timedelta,
) -> str:
    """Текст напоминания персоналу — без LLM (канон уведомления об отмене).

    Суть — оригиналом гостя: русский перевод персонал уже видел в исходном
    уведомлении, а звать модель из фоновой задачи ради повтора незачем. Номер,
    комната и категория — то, по чему служба действует.
    """
    if request.daily_number is not None:
        header = f"⏳ Заявку #{request.daily_number} никто не взял — {_humanized_age(age)}"
        action_line = (
            f"Ход: /start {request.daily_number} (взять в работу) · "
            f"/done {request.daily_number} · /cancel {request.daily_number}"
        )
    else:
        # Доскелетная заявка без номера (до миграции 0010) — фолбэк на id,
        # чтобы напоминание осталось действенным (канон уведомления о создании).
        header = f"⏳ Заявку никто не взял — {_humanized_age(age)}"
        action_line = f"id: {request.id}\nХод: /start · /done · /cancel + этот id."
    return "\n".join(
        [
            header,
            f"Категория: {category_name(category, request.category_id)}",
            f"Комната: {request.room_number or '—'}",
            f"Суть: {request.summary}",
            "",
            action_line,
        ]
    )


def _humanized_age(age: timedelta) -> str:
    """Возраст заявки короткой строкой: «45 мин», «2 ч 10 мин», «5 дн 3 ч».

    Сокращения вместо слов — чтобы не склонять числительные («1 час», «2 часа»,
    «5 часов») в четырёх местах. Крупная единица важнее точности: «5 дн» читается
    сразу, «123 ч 40 мин» требует деления в уме — а именно такие заявки и есть
    самые больные (комната 101 на staging висела пятые сутки).
    """
    total_minutes = max(int(age.total_seconds() // 60), 0)
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} дн" if not hours else f"{days} дн {hours} ч"
    if not hours:
        return f"{minutes} мин"
    return f"{hours} ч" if not minutes else f"{hours} ч {minutes} мин"
