"""Утреннее сообщение со сводкой дня (issue #301, spec 0035 §8).

Страница кабинета отвечает на вопрос «как дела прямо сейчас» — её надо открыть.
Сообщение приносит вчерашний итог тому, кто утром в кабинет не зайдёт: менеджеру
в чат, в 09:00 по времени отеля.

Механика — канон периодической задачи `remind_unclaimed_requests` (spec 0028 §4,
ADR-009): свой интервал в цикле воркера, никакого планировщика (NG-8). Раз в
`worker_daily_summary_interval_seconds` прогон обходит настроенных тенантов и у
каждого проверяет, наступило ли ЕГО локальное время рассылки.

Границы намеренно узкие:

- **ровно одно сообщение на сутки отеля каждому адресату** — ключи
  идемпотентности `staff:daily_summary:<день>` (отелю) и
  `platform:daily_summary:<день>` (копия основателю) на исходящих `Message`
  (P-8, тот же механизм, что у напоминаний). Ключей два, а не один, потому что
  адресата два и каждый может не получить сводку по своей причине: у отеля не
  задан чат, у копии — не настроен тракт алертов. Один ключ на оба означал бы,
  что копия уходит каждые десять минут до полуночи, пока `daily_summary_chat_id`
  тенанта пуст (issue #306);
- **без LLM** — как и напоминания: фоновая задача не должна ни стоить токенов,
  ни зависеть от доступности модели. Все числа — счётчики, весь текст — шаблон;
- **без ретрая по устройству** (§8): ключ пишется только после успешной
  отправки, поэтому следующий прогон в тот же день повторит попытку сам, а
  пропущенное утро видно на странице кабинета. Своей очереди у сводки нет.

Каждое число считает владелец своих данных, сводка их только складывает
(P-5, §6): заявки — `modules/requests.day_summary`, эскалации —
`channels/common.count_escalations`, расход на модель — `ai/gateway`. Окно суток
отеля берётся из ответа `day_summary`, а не считается здесь второй раз.

Мультитенантность (P-4): список тенантов — платформенной сессией, вся работа с
их данными — внутри `tenant_context` каждого.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Final

import structlog
from pydantic import BaseModel

from hospitality.ai.gateway import api as gateway
from hospitality.channels.common.events import count_escalations
from hospitality.channels.common.store import (
    ensure_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.channels.telegram.routing import current_correlation_id
from hospitality.modules.requests import api as requests_api
from hospitality.platform.config import (
    list_configured_tenant_ids,
    load_tenant_config,
    load_tenant_name,
)
from hospitality.shared.alerting import AlertSender
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.humanize import duration_label_ru, month_day_ru
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_daily_summary_sent
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8). Один на оба масштаба —
# сбой отправки одному адресату и падение сводки этого отеля целиком: действие
# дежурного и там и там одно (сводка за день не переотправляется, числа видны
# на странице кабинета), а различает их поле `recipient` в событии лога.
ERR_TELEGRAM_DAILY_SUMMARY_FAILED: Final = "ERR-TELEGRAM-007"

# Ключи идемпотентности (P-8). Третий сегмент — сутки отеля, ЗА КОТОРЫЕ сводка,
# а не дата отправки: ключ обязан называть то, о чём сообщение.
HOTEL_IDEMPOTENCY_KEY_PREFIX: Final = "staff:daily_summary"
COPY_IDEMPOTENCY_KEY_PREFIX: Final = "platform:daily_summary"

# Отправка одного сообщения: текст → внешний id сообщения (или None). Две
# реализации — бот отеля (`TelegramSender`) и бот команды (`send_alert`), и
# доставке всё равно, какая из них: она пишет `Message` после успеха любой.
_SendText = Callable[[str], Awaitable[str | None]]


class _SummaryBody(BaseModel):
    """Готовый текст сводки отеля и окно суток, за которое он посчитан.

    Окно нужно ещё и копии основателю: расход на модель считается ровно за те же
    границы, что и числа заявок (§6), и берётся оно из ответа `day_summary`, а не
    вычисляется здесь второй раз.
    """

    hotel_text: str
    day_start: datetime
    day_end: datetime


async def send_daily_summaries(
    *, sender: TelegramSender, alert_sender: AlertSender | None, alert_chat_id: str
) -> int:
    """Один прогон: разослать утренние сводки тем отелям, у кого настало время.

    Возвращает число ОТПРАВЛЕННЫХ сообщений (сводка отеля и копия основателю
    считаются отдельно). Зовётся из цикла воркера (`hospitality/worker.py`) и
    тестами напрямую. Прогон получает собственный `correlation_id` (§10.2):
    входящего запроса у фоновой задачи нет, а связать логи одного прогона с
    записанными им сообщениями надо. Сбой на одном тенанте не отменяет остальных.
    """
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        async with platform_session_scope() as session:
            tenant_ids = await list_configured_tenant_ids(session)

        sent = 0
        for tenant_id in tenant_ids:
            try:
                sent += await _send_for_tenant(
                    tenant_id,
                    sender=sender,
                    alert_sender=alert_sender,
                    alert_chat_id=alert_chat_id,
                )
            except Exception:
                # Сбой на одном отеле (битые данные, недоступная в этот момент
                # БД) не отменяет сводку у остальных: прогон обязан обойти всех.
                logger.error(
                    "daily_summary_failed",
                    error_code=ERR_TELEGRAM_DAILY_SUMMARY_FAILED,
                    tenant_id=str(tenant_id),
                    recipient="tenant_scan",
                    exc_info=True,
                )
                record_daily_summary_sent("failed")
                continue
        logger.info("daily_summaries_scanned", tenants=len(tenant_ids), sent=sent)
        return sent


async def _send_for_tenant(
    tenant_id: uuid.UUID,
    *,
    sender: TelegramSender,
    alert_sender: AlertSender | None,
    alert_chat_id: str,
) -> int:
    """Сводка одного отеля; вернуть, сколько сообщений ушло (0, 1 или 2)."""
    # tenant_id — явно: до входа в tenant_context его в лог-контексте нет, а без
    # него непонятно, у какого отеля не читается конфиг (§10.1).
    with structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)):
        try:
            async with platform_session_scope() as session:
                config = await load_tenant_config(session, tenant_id)
                tenant_name = await load_tenant_name(session, tenant_id)
        except AppError as error:
            # Дрейф схемы конфига или гонка с удалением тенанта: пропускаем его,
            # остальные отели прогон обязан обойти (канон `reminders.py`).
            logger.warning("daily_summary_config_unavailable", error_code=error.code)
            return 0

        local_now = utc_now().astimezone(config.tzinfo)
        if local_now.time() < config.daily_summary_at:
            return 0  # утро этого отеля ещё не наступило
        # Сводка — про ВЧЕРА: сообщение приносит закрытый итог, а не половину
        # текущего дня. Отсюда и «Вчера, 19 августа» первой строкой.
        service_day = local_now.date() - timedelta(days=1)

        hotel_chat_id = config.daily_summary_chat_id or ""
        hotel_key = f"{HOTEL_IDEMPOTENCY_KEY_PREFIX}:{service_day.isoformat()}"
        copy_key = f"{COPY_IDEMPOTENCY_KEY_PREFIX}:{service_day.isoformat()}"

        with tenant_context(tenant_id):
            hotel_pending = bool(hotel_chat_id) and not await notification_already_sent(hotel_key)
            copy_pending = (
                alert_sender is not None
                and bool(alert_chat_id)
                and not await notification_already_sent(copy_key)
            )
            if not hotel_pending and not copy_pending:
                # Ни одному адресату сегодня не должны — числа не считаем вовсе:
                # прогон повторяется каждые десять минут до полуночи, и холостая
                # сводка стоила бы четырёх запросов к БД на каждый из них.
                return 0

            body = await _summary_body(service_day)
            sent = 0
            if hotel_pending:
                sent += await _deliver(
                    text=body.hotel_text,
                    chat_id=hotel_chat_id,
                    idempotency_key=hotel_key,
                    recipient="hotel",
                    service_day=service_day,
                    send=lambda text: sender.send_message(hotel_chat_id, text),
                )
            if copy_pending and alert_sender is not None:  # второе — сужение типа для mypy
                spend_usd = await gateway.spend_usd_between(
                    created_after=body.day_start, created_before=body.day_end
                )
                sent += await _deliver(
                    text=_copy_text(body.hotel_text, tenant_name=tenant_name, spend_usd=spend_usd),
                    chat_id=alert_chat_id,
                    idempotency_key=copy_key,
                    recipient="founder",
                    service_day=service_day,
                    send=_alert_send(alert_sender),
                )
            return sent


def _alert_send(alert_sender: AlertSender) -> _SendText:
    """Адаптер тракта алертов к общей доставке: у `send_alert` нет message_id.

    Внешнего id у копии не будет никогда (прямой `sendMessage` ядра алертов
    ответа не разбирает), и это честнее, чем выдумать его: строка `Message`
    записывается с `external_message_id=None`, как у любого сообщения, чей id
    канал не узнал.
    """

    async def send(text: str) -> str | None:
        await alert_sender(text)
        return None

    return send


async def _deliver(
    *,
    text: str,
    chat_id: str,
    idempotency_key: str,
    recipient: str,
    service_day: date,
    send: _SendText,
) -> int:
    """Отправить одно сообщение и записать его; вернуть 1, если ушло.

    Запись — только ПОСЛЕ успешной отправки (не «соврать» историей), и она же
    гасит повтор: гонку двух воркеров закрывает уникальное ограничение
    `(tenant_id, idempotency_key)`. Сбой не пробрасывается — он не должен
    отменять второго адресата и остальных тенантов; следующий прогон повторит
    попытку сам, потому что ключа в БД нет (P-8).
    """
    try:
        conversation_id = await ensure_conversation(CHANNEL, chat_id)
        external_message_id = await send(text)
        await record_outbound_message(
            conversation_id,
            text,
            current_correlation_id(),
            external_message_id=external_message_id,
            idempotency_key=idempotency_key,
        )
    except Exception:
        logger.error(
            "daily_summary_failed",
            error_code=ERR_TELEGRAM_DAILY_SUMMARY_FAILED,
            recipient=recipient,
            chat_id=chat_id,
            service_day=service_day.isoformat(),
            exc_info=True,
        )
        record_daily_summary_sent("failed")
        return 0

    logger.info(
        "daily_summary_sent",
        recipient=recipient,
        chat_id=chat_id,
        service_day=service_day.isoformat(),
    )
    record_daily_summary_sent("sent")
    return 1


async def _summary_body(service_day: date) -> _SummaryBody:
    """Собрать числа у их владельцев и сложить в текст сводки отеля (§6, §9).

    Зовётся внутри `tenant_context`: оба владельца читают под RLS текущего
    тенанта, чужих чисел в сводке не окажется (P-4).
    """
    summary = await requests_api.day_summary(service_day)
    escalations = await count_escalations(
        created_after=summary.day_start, created_before=summary.day_end
    )
    category_names = {
        category.id: category.name for category in await requests_api.list_categories()
    }
    return _SummaryBody(
        hotel_text=_hotel_text(summary, escalations=escalations, category_names=category_names),
        day_start=summary.day_start,
        day_end=summary.day_end,
    )


def _hotel_text(
    summary: requests_api.RequestsDaySummary,
    *,
    escalations: int,
    category_names: dict[uuid.UUID, str],
) -> str:
    """Текст сводки отелю — надписи §9, без LLM (канон `_reminder_text`).

    Пустой день (все числа нули) печатается одной строкой: предикат тот же, что
    на странице кабинета, — своя половина у владельца чисел, эскалации сверху.
    Строка про расход на модель здесь отсутствует НАМЕРЕННО (решение основателя
    от 21.08.2026, §8): себестоимость платформы — не число отеля.
    """
    day = f"Вчера, {month_day_ru(summary.service_day)}"
    if summary.has_no_requests() and escalations == 0:
        return f"📊 {day}. Заявок не было."

    lines = [f"📊 {day}", _created_line(summary)]
    sources = _sources_line(summary)
    if sources is not None:
        lines.append(sources)
    if summary.by_service:
        lines.append("По службам, создано → закрыто:")
        lines.extend(
            f"• {category_names.get(row.category_id, '—')} — {row.created} → {row.closed}"
            for row in summary.by_service
        )
    lines.append(_median_line(summary.claim_median_seconds))
    lines.append(_overdue_line(summary.overdue))
    lines.append(f"Бот звал сотрудника: {escalations} {_times_word(escalations)}.")
    # «Открыто сейчас» — последней строкой и вне вчерашнего блока намеренно (§9):
    # единственное число сообщения, которое говорит про момент отправки, а не
    # про вчера. В 09:00 в него уже входят заявки сегодняшнего утреннего пика.
    lines.append(f"Открыто сейчас: {summary.open_now}.")
    return "\n".join(lines)


def _created_line(summary: requests_api.RequestsDaySummary) -> str:
    """«Заявки: 47 создано, 44 закрыто (41 выполнено, 3 отменено).»

    Разбивка закрытых — только когда закрытые есть: «0 закрыто (0 выполнено,
    0 отменено)» — три нуля вместо одного и ни слова сверх него.
    """
    if summary.closed_total == 0:
        return f"Заявки: {summary.created_total} создано, 0 закрыто."
    return (
        f"Заявки: {summary.created_total} создано, {summary.closed_total} закрыто "
        f"({summary.closed_done} выполнено, {summary.closed_cancelled} отменено)."
    )


def _sources_line(summary: requests_api.RequestsDaySummary) -> str | None:
    """«Откуда пришли: 39 — от гостей через бота, 8 — приняты вручную.»

    `None` — в этот день не создали ни одной: перечислять источники нечего.
    Третий источник печатается только ненулевым (issue #313, §9): сумма строки
    обязана сходиться с «создано», а до первой интеграции (#122) заявок через
    публичную дверь у отеля не будет, и «0 — из внешней системы» каждое утро
    объясняло бы менеджеру то, чего в его отеле нет.
    """
    if summary.created_total == 0:
        return None
    origin = summary.created_by_origin
    parts = [
        f"{origin[requests_api.ServiceRequestOrigin.GUEST_CHAT]} — от гостей через бота",
        f"{origin[requests_api.ServiceRequestOrigin.STAFF_MANUAL]} — приняты вручную",
    ]
    api_created = origin[requests_api.ServiceRequestOrigin.API]
    if api_created:
        parts.append(f"{api_created} — из внешней системы")
    return f"Откуда пришли: {', '.join(parts)}."


def _median_line(claim_median_seconds: int | None) -> str:
    """«Из тех, что брали в работу, половину взяли быстрее чем за 6 мин.»

    Слово «медиана» не звучит намеренно (§9): «половину взяли быстрее чем за N»
    — это ровно она, и её не нужно объяснять. Медианы не существует, когда в
    этот день не взяли ни одной заявки, — тогда предложение про «тех, что брали»
    не о чем, и вместо него стоит прямая констатация.
    """
    if claim_median_seconds is None:
        return "В работу в этот день не брали ни одной заявки."
    return (
        "Из тех, что брали в работу, половину взяли быстрее чем за "
        f"{duration_label_ru(claim_median_seconds)}."
    )


def _overdue_line(overdue: int | None) -> str:
    """«Просрочено за день: 3 — взяли позже срока или не взяли вовремя.»

    `None` — у отеля выключены напоминания, срока нет и просрочки не существует
    как явления (§6): ноль на этом месте читался бы как «сроки соблюдены».
    Расшифровка предиката дописывается только к ненулевому числу: «0 — взяли
    позже срока» — фраза, которая сама себе противоречит.
    """
    if overdue is None:
        return "Просрочено за день: не считается — напоминания выключены."
    if overdue == 0:
        return "Просрочено за день: 0."
    return f"Просрочено за день: {overdue} — взяли позже срока или не взяли вовремя."


def _times_word(count: int) -> str:
    """«раз» / «раза» — склонение при числе: 1 раз, 2 раза, 5 раз, 21 раз.

    Иначе каждое утро в чат менеджера приходит «звал 5 раз(а)». Правило русского
    счёта целиком: 11–14 — исключение, дальше решает последняя цифра.
    """
    if count % 100 in (11, 12, 13, 14):
        return "раз"
    return "раза" if count % 10 in (2, 3, 4) else "раз"


def _copy_text(hotel_text: str, *, tenant_name: str, spend_usd: Decimal) -> str:
    """Копия основателю: тот же текст, имя отеля первой строкой, расход последней.

    Имя отеля — потому что копий столько же, сколько отелей, и в одном чате они
    иначе неразличимы. Расход — потому что это себестоимость платформы, и адресат
    у неё один (§8, решение основателя от 21.08.2026); в сводке отеля этой строки
    нет. Печатается всегда, в том числе в пустой день: день без заявок, но с
    потраченными долларами — ровно то, о чём эта строка обязана сказать.
    """
    return f"🏨 {tenant_name}\n{hotel_text}\nИИ за сутки отеля: ${spend_usd:.2f}"
