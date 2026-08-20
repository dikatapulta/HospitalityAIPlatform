"""Воркер доменных событий — composition root второго процесса (Task 0010, ADR-005).

Тот же кодовый образ, что и API-приложение, другая точка входа (§5.3):
`python -m hospitality.worker`. Цикл: забрать пачку из outbox
(`deliver_pending_events`), доставить подписчикам; пустой outbox (или все
оставшиеся события ждут `next_attempt_at` — backoff, ADR-009) — спать
`worker_poll_interval_seconds`. Ошибка итерации целиком (БД недоступна,
миграции ещё не применены) не роняет процесс: логируется с ERR-EVENTS-003
и повторяется — `make dev`/деплой не обязаны угадывать порядок старта. А вот
ошибка конфигурации цикл не переживает: перед первой итерацией гоняется
`run_preflight()` (`preflight.py`) — список fail-fast проверок старта, куда
входит сверка `LLM_MODEL` с прайс-листом gateway (issue #137); с негодной
конфигурацией процесс не поднимается вовсе.

Тот же цикл на старте процесса и дальше раз в `worker_cleanup_interval_seconds`
вызывает `cleanup_terminal_events()` — retention-очистку завершённых строк
outbox (issue #18, ADR-009; issue #133, ADR-015); отдельная джоба/расписание не
заводятся (NG-8). Раз в `worker_dead_letter_alert_interval_seconds` —
`alert_dead_letter_events()`: событие, исчерпавшее повторы, обязано дойти до
человека алертом, а не остаться строкой ERROR в логе (issue #133).
Раз в `worker_heartbeat_interval_seconds` цикл отмечает пульс
(`record_worker_heartbeat()`, issue #136): о собственной смерти воркер доложить
не может, поэтому оставляет отметку времени, а алертит по ней watchdog снаружи
(ERR-OPS-003). Пульс — первый из периодических шагов: он говорит «круг цикла
состоялся», и зависшая доставка или зависший шаг ниже до него не доходят.
Тем же способом раз в `worker_reminder_interval_seconds` вызывается
`remind_unclaimed_requests()` — напоминание службе о заявках, которые никто не
взял дольше срока тенанта (issue #57, spec 0028), а раз в
`worker_retention_interval_seconds` — `enforce_guest_text_retention()` — ретеншн
гостевых текстов: 90 дней, обещание политики конфиденциальности (issue #42,
spec 0032).

Подписчики регистрируются здесь явно — это аналог include_router в app.py:
новый модуль добавляет свою пару (событие, обработчик) в register_subscribers.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from hospitality.channels.common.retention import (
    ERR_CHANNEL_RETENTION_FAILED,
    enforce_guest_text_retention,
)
from hospitality.channels.telegram import notifications as telegram_notifications
from hospitality.channels.telegram import redelivery as telegram_redelivery
from hospitality.channels.telegram.client import TelegramSender, build_telegram_sender
from hospitality.channels.telegram.reminders import (
    ERR_TELEGRAM_REMINDER_SCAN_FAILED,
    remind_unclaimed_requests,
)
from hospitality.platform.events import CanaryCreated, echo_canary_created
from hospitality.preflight import run_preflight
from hospitality.shared.alerting import AlertSender, alerting_configured, send_alert
from hospitality.shared.config import get_settings
from hospitality.shared.db import utc_now
from hospitality.shared.events import (
    cleanup_terminal_events,
    deliver_pending_events,
    subscribe,
)
from hospitality.shared.heartbeat import record_worker_heartbeat
from hospitality.shared.logging import configure_logging, get_logger
from hospitality.shared.outbox_alerts import alert_dead_letter_events
from hospitality.shared.sentry import init_sentry

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_WORKER_ITERATION_FAILED = "ERR-EVENTS-003"  # итерация воркера упала целиком
ERR_EVENTS_CLEANUP_FAILED = "ERR-EVENTS-004"  # retention-очистка outbox упала
ERR_EVENTS_DEAD_LETTER_ALERT_FAILED = "ERR-EVENTS-005"  # алерт о dead-letter не ушёл
ERR_WORKER_HEARTBEAT_FAILED = "ERR-OPS-005"  # пульс воркера не записался
# Код прогона напоминаний живёт рядом с самой задачей (reminders.py) — второе
# определение той же строки разъехалось бы при правке.


def register_subscribers(sender: TelegramSender) -> None:
    """Единственное место подключения подписчиков к событиям (P-12).

    Новый потребитель события = строка здесь + обработчик в своём слое; модуль,
    публикующий событие, о подписчиках не знает (P-6). Аналог `include_router`.
    """
    subscribe(CanaryCreated, echo_canary_created)
    # Уведомления Telegram (Task 0017): служба узнаёт о новой заявке, гость —
    # о её выполнении. Отправитель и ДЕФОЛТНЫЙ staff-чат берутся из настроек
    # окружения; чат конкретной службы подписчик выбирает по категории заявки
    # из конфига тенанта (spec 0026).
    telegram_notifications.register(
        sender=sender,
        default_staff_chat_id=get_settings().telegram_staff_chat_id,
    )
    # Дожим ответа в чат (гостю или персоналу), не ушедшего с первой попытки
    # (issue #209): реплику кладёт в outbox процесс app (обработчик вебхука),
    # дожимает — этот процесс.
    telegram_redelivery.register(sender=sender)


def build_dead_letter_alert_sender() -> AlertSender | None:
    """Отправитель алертов о dead-letter; None — тракт алертов не настроен.

    Отдельно от гостевого `TelegramSender`: у алертов свои бот и чат команды
    (`shared/alerting.py`), а не чат службы отеля. Без конфигурации шаг
    пропускается — dev и CI не обязаны иметь Telegram, но и метку «человеку
    сказано» тогда никто не поставит: похороненные события дождутся настройки.
    """
    settings = get_settings()
    if not alerting_configured(settings):  # fail-fast на полузаполненной паре — внутри
        logger.warning(
            "dead_letter_alerting_disabled",
            reason="TELEGRAM_ALERT_BOT_TOKEN/TELEGRAM_ALERT_CHAT_ID не заданы",
            runbook="docs/runbooks/alerts.md",
        )
        return None

    async def send(text: str) -> None:
        await send_alert(get_settings(), text)

    return send


async def run_worker(iterations: int | None = None) -> None:
    """Цикл воркера. `iterations` ограничивает число итераций (для тестов)."""
    # Конфигурация — до первой итерации (issue #137): образ кода и .env у двух
    # процессов общие, и подняться с негодной конфигурацией не вправе ни один.
    # Проверка здесь, а не в main(): сборка воркера (отправитель, подписчики)
    # живёт в run_worker — там же, где её видят тесты. В контейнере тот же
    # список проверок отрабатывает раньше — в ENTRYPOINT образа (issue #267).
    run_preflight()
    # Отправитель Telegram — один на процесс: его делят подписчики-уведомления
    # и напоминания о невзятых заявках (spec 0028).
    sender = build_telegram_sender(get_settings())
    register_subscribers(sender)
    alert_sender = build_dead_letter_alert_sender()
    completed = 0
    # Первая очистка — сразу на старте процесса: иначе воркер, рестартующий
    # чаще worker_cleanup_interval_seconds (частые деплои), не выполнит
    # retention ни разу (ревью PR #19, находка 1). То же и у напоминаний.
    last_cleanup_at = utc_now() - timedelta(seconds=get_settings().worker_cleanup_interval_seconds)
    last_reminder_at = utc_now() - timedelta(
        seconds=get_settings().worker_reminder_interval_seconds
    )
    last_retention_at = utc_now() - timedelta(
        seconds=get_settings().worker_retention_interval_seconds
    )
    last_dead_letter_alert_at = utc_now() - timedelta(
        seconds=get_settings().worker_dead_letter_alert_interval_seconds
    )
    # Пульс — сразу первым кругом: между стартом процесса и первой отметкой
    # воркер для watchdog'а неотличим от мёртвого, и на медленном старте это
    # был бы ложный алерт.
    last_heartbeat_at = utc_now() - timedelta(
        seconds=get_settings().worker_heartbeat_interval_seconds
    )
    while iterations is None or completed < iterations:
        completed += 1
        try:
            processed = await deliver_pending_events()
        except Exception:  # инфраструктурный сбой итерации — ждём и повторяем
            logger.error(
                "worker_iteration_failed",
                error_code=ERR_WORKER_ITERATION_FAILED,
                exc_info=True,
            )
            processed = 0

        now = utc_now()
        heartbeat_interval = get_settings().worker_heartbeat_interval_seconds
        if (now - last_heartbeat_at).total_seconds() >= heartbeat_interval:
            try:
                await record_worker_heartbeat()
            except Exception:  # пульс не записался — цикл не роняем, см. ERR-OPS-005
                logger.error(
                    "worker_heartbeat_failed",
                    error_code=ERR_WORKER_HEARTBEAT_FAILED,
                    exc_info=True,
                )
            last_heartbeat_at = now

        cleanup_interval = get_settings().worker_cleanup_interval_seconds
        if (now - last_cleanup_at).total_seconds() >= cleanup_interval:
            try:
                await cleanup_terminal_events()
            except Exception:  # retention — не критичный путь доставки, не роняем цикл
                logger.error(
                    "outbox_cleanup_failed",
                    error_code=ERR_EVENTS_CLEANUP_FAILED,
                    exc_info=True,
                )
            last_cleanup_at = now

        alert_interval = get_settings().worker_dead_letter_alert_interval_seconds
        if alert_sender is not None and (
            (now - last_dead_letter_alert_at).total_seconds() >= alert_interval
        ):
            try:
                await alert_dead_letter_events(alert_sender)
            except Exception:  # Telegram лежит — не роняем доставку; пачка уйдёт позже
                logger.error(
                    "dead_letter_alert_failed",
                    error_code=ERR_EVENTS_DEAD_LETTER_ALERT_FAILED,
                    exc_info=True,
                )
            last_dead_letter_alert_at = now

        reminder_interval = get_settings().worker_reminder_interval_seconds
        if (now - last_reminder_at).total_seconds() >= reminder_interval:
            try:
                await remind_unclaimed_requests(
                    sender=sender,
                    default_staff_chat_id=get_settings().telegram_staff_chat_id,
                )
            except Exception:  # напоминания — не критичный путь доставки, не роняем цикл
                logger.error(
                    "unclaimed_request_scan_failed",
                    error_code=ERR_TELEGRAM_REMINDER_SCAN_FAILED,
                    exc_info=True,
                )
            last_reminder_at = now

        retention_interval = get_settings().worker_retention_interval_seconds
        if (now - last_retention_at).total_seconds() >= retention_interval:
            try:
                await enforce_guest_text_retention()
            except Exception:  # ретеншн — не критичный путь доставки, не роняем цикл
                logger.error(
                    "guest_text_retention_failed",
                    error_code=ERR_CHANNEL_RETENTION_FAILED,
                    exc_info=True,
                )
            last_retention_at = now

        if processed == 0:
            await asyncio.sleep(get_settings().worker_poll_interval_seconds)


def main() -> None:  # pragma: no cover — точка входа процесса; логика покрыта run_worker
    configure_logging(get_settings().log_level)
    # Sentry воркера (Task 0018, §10.4): ERROR-логи (worker_iteration_failed
    # и т.п.) и падение процесса становятся событиями с контекстом тенанта.
    init_sentry(get_settings())
    logger.info("worker_started")
    asyncio.run(run_worker())


if __name__ == "__main__":  # pragma: no cover
    main()
