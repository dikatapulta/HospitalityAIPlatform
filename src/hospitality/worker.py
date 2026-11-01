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
взял дольше срока тенанта (issue #57, spec 0028), и `remind_stalled_requests()` —
о взятых и не закрытых (issue #120, spec 0028 §8), а раз в
`worker_retention_interval_seconds` — `enforce_guest_text_retention()` — ретеншн
гостевых текстов: 90 дней, обещание политики конфиденциальности (issue #42,
spec 0032). Раз в `worker_daily_summary_interval_seconds` —
`send_daily_summaries()`: у каждого отеля проверяется, наступило ли ЕГО
локальное время утренней рассылки, и наступившим уходит сводка вчерашнего дня
в чат менеджера плюс копия в канал команды (issue #301, spec 0035 §8).
Раз в `worker_stay_auto_checkout_interval_seconds` — `close_overdue_stays()`:
автовыезд Stay, у которых вышел запас после расчётного часа (issue #225,
spec 0043) — иначе забытый выезд держал бы комнату занятой вечно.
Планировщика по-прежнему нет (NG-8): «в 09:00 по времени отеля» — это условие
внутри обычного шага цикла, а не расписание снаружи.

Рядом с циклом, в том же процессе, работает вторая задача — медленная полоса
(`run_slow_lane`, issue #511): события `SLOW_LANE_EVENTS`, подписчик которых
держит дочерний процесс песочницы (разбор реквизитов для АВР, spec 0049 Р-4).
Пачка общего цикла доставляется по одному, и враждебный PDF держал бы её
минутами: эскалация ЧП ждала бы, пульс пропускал бы порог. Цикл такие события
не берёт, полоса — только их; outbox, повторы и dead-letter у обеих общие.
Третья задача — рассылка пуша на телефоны персонала (`run_push_lane`,
spec 0050 §6): раз в `worker_staff_push_interval_seconds` зовёт телефоны о том
же, о чём вкладка кабинета. Своя задача по той же причине: служба пуша, которая
не отвечает по 10 с, не должна держать доставку событий. Пуш выключен на
контуре (`STAFF_PUSH_VAPID_PRIVATE_KEY` пуст) — задачи нет.

Подписчики регистрируются здесь явно — это аналог include_router в app.py:
новый модуль добавляет свою пару (событие, обработчик) в register_subscribers.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Final

from hospitality.ai import act_requisites
from hospitality.channels.common.retention import (
    ERR_CHANNEL_RETENTION_FAILED,
    enforce_guest_text_retention,
)
from hospitality.channels.telegram import notifications as telegram_notifications
from hospitality.channels.telegram import redelivery as telegram_redelivery
from hospitality.channels.telegram.client import TelegramSender, build_telegram_sender
from hospitality.channels.telegram.daily_summary import (
    ERR_TELEGRAM_DAILY_SUMMARY_FAILED,
    send_daily_summaries,
)
from hospitality.channels.telegram.reminders import (
    ERR_TELEGRAM_REMINDER_SCAN_FAILED,
    remind_unclaimed_requests,
)
from hospitality.channels.telegram.stalled_reminders import remind_stalled_requests
from hospitality.channels.web.documents import messages as act_request_messages
from hospitality.channels.web.menu import messages as room_service_messages
from hospitality.integrations.file_storage import build_file_storage
from hospitality.integrations.pms import build_pms
from hospitality.integrations.web_push import build_web_push
from hospitality.modules.documents import api as documents_api
from hospitality.modules.guests import api as guests_api
from hospitality.platform.events import CanaryCreated, SuperadminActed, echo_canary_created
from hospitality.platform.superadmin_alerts import build_superadmin_alert_handler
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
from hospitality.shared.ports.file_storage import set_file_storage
from hospitality.shared.ports.pms import set_pms
from hospitality.shared.ports.web_push import set_web_push, web_push_or_none
from hospitality.shared.sentry import init_sentry
from hospitality.staff_portal.push_sweep import ERR_PUSH_SWEEP_FAILED, run_push_sweep

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_WORKER_ITERATION_FAILED = "ERR-EVENTS-003"  # итерация воркера упала целиком
ERR_EVENTS_CLEANUP_FAILED = "ERR-EVENTS-004"  # retention-очистка outbox упала
ERR_EVENTS_DEAD_LETTER_ALERT_FAILED = "ERR-EVENTS-005"  # алерт о dead-letter не ушёл
ERR_WORKER_HEARTBEAT_FAILED = "ERR-OPS-005"  # пульс воркера не записался
# Код прогона напоминаний живёт рядом с самой задачей (reminders.py) — второе
# определение той же строки разъехалось бы при правке.

# Медленная полоса (issue #511): события, подписчик которых держит дочерний
# процесс песочницы секунды, а на враждебном файле — минуты. Общий цикл их не
# берёт — их доставляет `run_slow_lane`. Новое медленное событие — строка здесь.
SLOW_LANE_EVENTS: Final = frozenset({documents_api.ActRequestSubmitted.event_name})


def register_subscribers(sender: TelegramSender, alert_sender: AlertSender | None = None) -> None:
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
    # Вход и действия суперадмина — уведомление основателю алерт-ботом
    # (spec 0044 §6): событие пишется в одной транзакции с действием, а пара
    # TELEGRAM_ALERT_* есть только у этого процесса.
    subscribe(SuperadminActed, build_superadmin_alert_handler(alert_sender))
    # «Готово» и «Вернули» гостю о реквизитах для АВР (spec 0049 Р-2): свой
    # подписчик — у запроса нет `request_origins`, общий уведомитель его не видит.
    act_request_messages.register()
    # Разбор реквизитов для АВР (spec 0049 Р-4): PDF и DOCX гостя читаются здесь,
    # в дочернем процессе песочницы, а не в процессе, обслуживающем гостей; затем
    # фото — моделью, если задан `LLM_MODEL_DOCUMENTS` (Р-13). Событие — в
    # медленной полосе (`SLOW_LANE_EVENTS`): общий цикл его не ждёт.
    subscribe(documents_api.ActRequestSubmitted, act_requisites.handle_act_request_submitted)
    # Сообщения гостю о шагах заказа в номер (spec 0038 §5, Р-8): свой подписчик
    # на события заявки — общий уведомитель гостя заказ не видит (нет в
    # `request_origins`), а этот молчит обо всём, что не заказ.
    room_service_messages.register()


def build_alert_sender() -> AlertSender | None:
    """Отправитель в канал команды; None — тракт алертов не настроен.

    Отдельно от гостевого `TelegramSender`: у алертов свои бот и чат команды
    (`shared/alerting.py`), а не чат службы отеля. Без конфигурации шаг
    пропускается — dev и CI не обязаны иметь Telegram, но и метку «человеку
    сказано» тогда никто не поставит: похороненные события дождутся настройки.

    Потребителей три, поэтому имя общее: алерт о событиях в dead-letter
    (issue #133), копия утренней сводки основателю (issue #301, spec 0035 §8)
    и уведомления о суперадмине (spec 0044 §6).
    Второй — не про поломку, но адресат и тракт у него те же, и заводить второй
    отправитель к тому же боту значило бы разойтись с ним при первой же правке.
    """
    settings = get_settings()
    if not alerting_configured(settings):  # fail-fast на полузаполненной паре — внутри
        logger.warning(
            "alerting_disabled",
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
    # Хранилище файлов (spec 0047): ретеншн удаляет файлы гостя — воркер тоже
    # composition root и сам кладёт адаптер в порт, как app.py.
    set_file_storage(build_file_storage(get_settings()))
    # Веб-пуш (spec 0050 §6): рассылку на телефоны персонала шлёт этот процесс.
    set_web_push(build_web_push(get_settings()))
    # Источник проживаний (spec 0058 §2.1): сверку со снимком ведёт этот процесс (PR 3).
    set_pms(build_pms())
    # Отправитель Telegram — один на процесс: его делят подписчики-уведомления
    # и напоминания о невзятых заявках (spec 0028).
    sender = build_telegram_sender(get_settings())
    alert_sender = build_alert_sender()
    register_subscribers(sender, alert_sender)
    lanes = [asyncio.create_task(run_slow_lane())]
    if web_push_or_none() is not None:
        lanes.append(asyncio.create_task(run_push_lane()))
    try:
        await _run_cycle(iterations, sender, alert_sender)
    finally:
        for lane in lanes:
            lane.cancel()
        for lane in lanes:
            with contextlib.suppress(asyncio.CancelledError):
                await lane


async def run_slow_lane() -> None:
    """Медленная полоса (issue #511): события `SLOW_LANE_EVENTS` — по одному, своей задачей.

    Задача живёт рядом с циклом в том же процессе и на том же event loop:
    подписчик ждёт дочерний процесс, не занимая цикл, и эскалации, уведомления
    и дожим ответов идут, пока разбираются файлы. Повторы, backoff и
    dead-letter — те же, что у общей очереди (outbox). По одному — чтобы
    аренда `locked_until` держала только событие, которое правда в работе.
    """
    while True:
        try:
            processed = await deliver_pending_events(batch_size=1, only_events=SLOW_LANE_EVENTS)
        except Exception:  # инфраструктурный сбой — ждём и повторяем, как общий цикл
            logger.error(
                "worker_iteration_failed",
                error_code=ERR_WORKER_ITERATION_FAILED,
                lane="slow",
                exc_info=True,
            )
            processed = 0
        if processed == 0:
            await asyncio.sleep(get_settings().worker_poll_interval_seconds)


async def run_push_lane() -> None:
    """Рассылка пуша (spec 0050 §6): проход раз в интервал, первый — через интервал.

    Сбои отелей и служб пуша проход пишет сам; здесь — только то, что вырвалось
    из него целиком: задача обязана жить, пока жив процесс.
    """
    while True:
        await asyncio.sleep(get_settings().worker_staff_push_interval_seconds)
        try:
            await run_push_sweep()
        except Exception:
            logger.error("staff_push.sweep_failed", error_code=ERR_PUSH_SWEEP_FAILED, exc_info=True)


async def _run_cycle(
    iterations: int | None, sender: TelegramSender, alert_sender: AlertSender | None
) -> None:
    """Общий цикл: доставка всех событий, кроме медленной полосы, и периодические шаги."""
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
    last_daily_summary_at = utc_now() - timedelta(
        seconds=get_settings().worker_daily_summary_interval_seconds
    )
    last_stay_auto_checkout_at = utc_now() - timedelta(
        seconds=get_settings().worker_stay_auto_checkout_interval_seconds
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
            processed = await deliver_pending_events(skip_events=SLOW_LANE_EVENTS)
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
            try:
                await remind_stalled_requests(
                    sender=sender,
                    default_staff_chat_id=get_settings().telegram_staff_chat_id,
                )
            except Exception:  # тот же класс шага, отдельно: сбой одного не глушит другое
                logger.error(
                    "stalled_request_scan_failed",
                    error_code=ERR_TELEGRAM_REMINDER_SCAN_FAILED,
                    exc_info=True,
                )
            last_reminder_at = now

        daily_summary_interval = get_settings().worker_daily_summary_interval_seconds
        if (now - last_daily_summary_at).total_seconds() >= daily_summary_interval:
            try:
                await send_daily_summaries(
                    sender=sender,
                    alert_sender=alert_sender,
                    alert_chat_id=get_settings().telegram_alert_chat_id,
                )
            except Exception:  # сводка — не критичный путь доставки, не роняем цикл
                logger.error(
                    "daily_summary_failed",
                    error_code=ERR_TELEGRAM_DAILY_SUMMARY_FAILED,
                    recipient="run",
                    exc_info=True,
                )
            last_daily_summary_at = now

        stay_auto_checkout_interval = get_settings().worker_stay_auto_checkout_interval_seconds
        if (now - last_stay_auto_checkout_at).total_seconds() >= stay_auto_checkout_interval:
            try:
                await guests_api.close_overdue_stays()
            except Exception:  # автовыезд — не путь доставки, не роняем цикл
                logger.error(
                    "stay_auto_checkout_failed",
                    error_code=guests_api.ERR_GUESTS_AUTO_CHECKOUT_FAILED,
                    exc_info=True,
                )
            last_stay_auto_checkout_at = now

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
