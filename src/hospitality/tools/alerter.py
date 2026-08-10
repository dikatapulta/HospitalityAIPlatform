"""Алертер: watchdog staging → Telegram-канал команды (Task 0018, §10.8).

Четвёртый процесс staging-стека — тот же образ приложения, другая команда
(канон «один образ, другая точка входа», §5.3, как ``hospitality.worker``):

    python -m hospitality.tools.alerter

Цикл раз в ``ALERT_POLL_INTERVAL_SECONDS``:

1. ``GET /health/ready``. Не-200 или сетевая ошибка
   ``ALERT_READY_FAILURE_THRESHOLD`` опросов подряд → алерт **ERR-OPS-001**
   (однократно, до восстановления); первый успех после алерта → сообщение
   о восстановлении.
2. ``GET /metrics``: прирост суммы ``http_requests_total{status="5xx"}`` за
   интервал ≥ ``ALERT_ERROR_SPIKE_THRESHOLD`` → алерт **ERR-OPS-002**, не чаще
   ``ALERT_COOLDOWN_SECONDS``. Недоступный ``/metrics`` — пропуск шага:
   падение приложения целиком уже покрыто ERR-OPS-001.
3. Оттуда же ``worker_heartbeat_age_seconds`` старше
   ``ALERT_WORKER_HEARTBEAT_MAX_AGE_SECONDS`` → алерт **ERR-OPS-003**: воркер
   мёртв или завис, и вся доставка событий стоит молча (issue #136). Проверяет
   именно watchdog, а не сам воркер: о своей смерти процесс не докладывает.
4. И ``outbox_pending_events`` выше ``ALERT_OUTBOX_DEPTH_THRESHOLD`` → алерт
   **ERR-OPS-004**. Вторая линия на тот же симптом: она ловит случай, которого
   пульс не видит, — цикл жив и пульс свежий, но очередь не разбирается
   (доставки падают по кругу, воркер не успевает).

Алерты состояния (1, 3, 4) устроены одинаково: одно сообщение на вход в
проблему и ✅ на выход, а не лента каждую минуту. Пустая метрика (NaN — БД
недоступна, или /metrics не ответил) — это «не знаю», а не «всё хорошо»:
шаг молча пропускается, потому что такой отказ уже покрыт ERR-OPS-001.

Формат текста и отправка — общий канон ``shared/alerting.py`` (им же
докладывает воркер о dead-letter, issue #133); состояние — в памяти
(перезапуск = худший случай один повторный алерт), БД и Redis не нужны.

Оба ``TELEGRAM_ALERT_*`` пустые — алертер пассивен (WARNING раз в час):
деплой без настроенных алертов остаётся зелёным, неконфигурация видна в
логах. Заполнен только один — ошибка конфигурации, немедленное падение.

Известное ограничение (docs/runbooks/alerts.md): алертер живёт на том же
VPS — смерть всего сервера не заалертит; лечится внешним uptime-сервисом
(managed, §10.12), вне DoD Task 0018.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import httpx

from hospitality.shared.alerting import alert_request, alerting_configured, format_alert
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.logging import configure_logging, get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_READY_UNAVAILABLE = "ERR-OPS-001"
ERR_ERROR_SPIKE = "ERR-OPS-002"
ERR_WORKER_STALLED = "ERR-OPS-003"
ERR_OUTBOX_BACKLOG = "ERR-OPS-004"

_HTTP_TIMEOUT_SECONDS = 5.0
_DISABLED_REMINDER_SECONDS = 3600.0


@dataclass(frozen=True)
class ProbeResult:
    """Снимок одного опроса приложения.

    Все три значения из /metrics — `None`, когда их нет: эндпоинт не ответил
    или метрика пуста (NaN, БД недоступна). «Нет данных» ≠ «ноль».
    """

    ready_ok: bool
    ready_detail: str
    # Сумма счётчиков 5xx из /metrics; None — /metrics недоступен.
    server_error_total: float | None
    # Возраст пульса воркера и глубина очереди outbox (issue #136).
    # Без умолчания `= None` намеренно: «нет данных» — это молчание алерта, и
    # забытое поле выключило бы линию тихо. Теперь оно ошибка mypy.
    worker_heartbeat_age_seconds: float | None
    outbox_pending_events: float | None


def sum_server_errors(metrics_text: str) -> float:
    """Сумма ``http_requests_total{...status="5xx"...}`` по всем маршрутам."""
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith("http_requests_total{") and 'status="5xx"' in line:
            total += float(line.rsplit(" ", 1)[-1])
    return total


def read_gauge(metrics_text: str, name: str) -> float | None:
    """Значение gauge без лейблов из выдачи ``/metrics``; None — нет значения.

    None отдаётся и когда метрики в тексте нет (старая версия приложения), и
    когда она NaN (``shared/metrics.py`` пишет так «не знаю»: БД недоступна).
    Оба случая для алертов одинаковы — молчать, а не считать нулём.
    """
    prefix = f"{name} "
    for line in metrics_text.splitlines():
        if line.startswith(prefix):
            value = float(line[len(prefix) :])
            return None if math.isnan(value) else value
    return None


def _humanize_seconds(seconds: float) -> str:
    """«42 сек» / «7 мин» — возраст и порог в тексте, который читает человек."""
    if seconds < 90:
        return f"{int(seconds)} сек"
    return f"{round(seconds / 60)} мин"


@dataclass
class AlertMonitor:
    """Машина состояний алертов — чистая логика, без I/O (тестируется без сети)."""

    ready_failure_threshold: int
    error_spike_threshold: int
    cooldown_seconds: float
    environment: str
    runbook_url: str
    worker_heartbeat_max_age_seconds: float
    outbox_depth_threshold: int

    consecutive_ready_failures: int = 0
    ready_alert_active: bool = False
    last_server_error_total: float | None = None
    last_spike_alert_at: float | None = None
    heartbeat_alert_active: bool = False
    outbox_depth_alert_active: bool = False

    def evaluate(self, probe: ProbeResult, *, now: float) -> list[str]:
        """Обработать снимок опроса; вернуть сообщения, которые пора отправить.

        ``now`` — монотонные секунды (``time.monotonic()``): cooldown не должен
        зависеть от перевода системных часов.
        """
        return (
            self._evaluate_ready(probe)
            + self._evaluate_error_spike(probe, now=now)
            + self._evaluate_worker_heartbeat(probe)
            + self._evaluate_outbox_depth(probe)
        )

    def _evaluate_ready(self, probe: ProbeResult) -> list[str]:
        if probe.ready_ok:
            recovered = self.ready_alert_active
            self.ready_alert_active = False
            self.consecutive_ready_failures = 0
            if recovered:
                return [
                    format_alert(
                        error_code=ERR_READY_UNAVAILABLE,
                        title="/health/ready снова отвечает",
                        detail=probe.ready_detail,
                        environment=self.environment,
                        runbook_url=self.runbook_url,
                        emoji="✅",
                    )
                ]
            return []
        self.consecutive_ready_failures += 1
        if (
            self.ready_alert_active
            or self.consecutive_ready_failures < self.ready_failure_threshold
        ):
            return []
        self.ready_alert_active = True
        return [
            format_alert(
                error_code=ERR_READY_UNAVAILABLE,
                title=(
                    f"/health/ready недоступен или нездоров "
                    f"{self.consecutive_ready_failures} опроса(ов) подряд"
                ),
                detail=probe.ready_detail,
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]

    def _evaluate_error_spike(self, probe: ProbeResult, *, now: float) -> list[str]:
        if probe.server_error_total is None:
            return []
        previous_total = self.last_server_error_total
        self.last_server_error_total = probe.server_error_total
        if previous_total is None:
            return []  # первый опрос — только базовая линия
        delta = probe.server_error_total - previous_total
        if delta < 0:
            # Счётчик обнулился (процесс приложения перезапустился) —
            # всплеском считаем накопленное с нуля.
            delta = probe.server_error_total
        if delta < self.error_spike_threshold:
            return []
        if (
            self.last_spike_alert_at is not None
            and now - self.last_spike_alert_at < self.cooldown_seconds
        ):
            return []
        self.last_spike_alert_at = now
        return [
            format_alert(
                error_code=ERR_ERROR_SPIKE,
                title="всплеск ошибок 5xx",
                detail=f"+{delta:g} ответов 5xx за интервал опроса",
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]

    def _evaluate_worker_heartbeat(self, probe: ProbeResult) -> list[str]:
        """Пульс воркера старше порога — ERR-OPS-003 (issue #136).

        Алерт состояния, как ERR-OPS-001: одно сообщение на вход в проблему и
        ✅ на выход. «Возраста нет» (None) — молчим: это либо недоступная БД
        (уже ERR-OPS-001), либо приложение старой версии без метрики.
        """
        age = probe.worker_heartbeat_age_seconds
        if age is None:
            return []
        if age <= self.worker_heartbeat_max_age_seconds:
            if not self.heartbeat_alert_active:
                return []
            self.heartbeat_alert_active = False
            return [
                format_alert(
                    error_code=ERR_WORKER_STALLED,
                    title="воркер событий снова подаёт признаки жизни",
                    detail=f"последний пульс {_humanize_seconds(age)} назад",
                    environment=self.environment,
                    runbook_url=self.runbook_url,
                    emoji="✅",
                )
            ]
        if self.heartbeat_alert_active:
            return []
        self.heartbeat_alert_active = True
        threshold = _humanize_seconds(self.worker_heartbeat_max_age_seconds)
        return [
            format_alert(
                error_code=ERR_WORKER_STALLED,
                title="воркер событий не подаёт признаков жизни",
                detail=(
                    f"последний пульс {_humanize_seconds(age)} назад (порог {threshold}) — "
                    "доставка событий стоит: уведомления службам, напоминания "
                    "и эскалации не идут"
                ),
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]

    def _evaluate_outbox_depth(self, probe: ProbeResult) -> list[str]:
        """Очередь outbox выше порога — ERR-OPS-004 (вторая линия, issue #136)."""
        depth = probe.outbox_pending_events
        if depth is None:
            return []
        if depth <= self.outbox_depth_threshold:
            if not self.outbox_depth_alert_active:
                return []
            self.outbox_depth_alert_active = False
            return [
                format_alert(
                    error_code=ERR_OUTBOX_BACKLOG,
                    title="очередь событий вернулась в норму",
                    detail=f"сейчас в очереди {depth:g} (порог {self.outbox_depth_threshold})",
                    environment=self.environment,
                    runbook_url=self.runbook_url,
                    emoji="✅",
                )
            ]
        if self.outbox_depth_alert_active:
            return []
        self.outbox_depth_alert_active = True
        return [
            format_alert(
                error_code=ERR_OUTBOX_BACKLOG,
                title="очередь событий растёт",
                detail=(
                    f"сейчас в очереди {depth:g} "
                    f"(порог {self.outbox_depth_threshold}) — "
                    "воркер стоит или не успевает"
                ),
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]


def probe_application(client: httpx.Client, base_url: str) -> ProbeResult:
    """Один опрос приложения: /health/ready и /metrics."""
    try:
        ready_response = client.get(f"{base_url}/health/ready")
        ready_ok = ready_response.status_code == 200
        ready_detail = ready_response.text.strip()
    except httpx.HTTPError as error:  # диагностический путь: сбой — это статус
        ready_ok = False
        ready_detail = f"connection error: {error}"

    metrics_text: str | None
    try:
        metrics_response = client.get(f"{base_url}/metrics")
        metrics_text = metrics_response.text if metrics_response.status_code == 200 else None
    except httpx.HTTPError:
        metrics_text = None

    if metrics_text is None:
        return ProbeResult(
            ready_ok=ready_ok,
            ready_detail=ready_detail,
            server_error_total=None,
            worker_heartbeat_age_seconds=None,
            outbox_pending_events=None,
        )
    return ProbeResult(
        ready_ok=ready_ok,
        ready_detail=ready_detail,
        server_error_total=sum_server_errors(metrics_text),
        worker_heartbeat_age_seconds=read_gauge(metrics_text, "worker_heartbeat_age_seconds"),
        outbox_pending_events=read_gauge(metrics_text, "outbox_pending_events"),
    )


def send_telegram_message(client: httpx.Client, settings: Settings, text: str) -> None:
    """Отправить сообщение в Telegram-канал команды; сбой отправки логируется,
    но не роняет цикл (следующая итерация попробует снова при новом алерте)."""
    url, payload = alert_request(settings, text)
    try:
        response = client.post(url, json=payload)
        response.raise_for_status()
        logger.info("alert_sent", text=text)
    except httpx.HTTPError:
        logger.error("alert_send_failed", text=text, exc_info=True)


def run_alerter(
    iterations: int | None = None, *, transport: httpx.BaseTransport | None = None
) -> None:
    """Цикл алертера. ``iterations``/``transport`` — только для тестов."""
    settings = get_settings()
    # Fail-fast (§11) на полузаполненной паре — внутри alerting_configured.
    if not alerting_configured(settings):
        _run_disabled_loop(iterations)
        return

    monitor = AlertMonitor(
        ready_failure_threshold=settings.alert_ready_failure_threshold,
        error_spike_threshold=settings.alert_error_spike_threshold,
        cooldown_seconds=settings.alert_cooldown_seconds,
        environment=settings.sentry_environment,
        runbook_url=settings.alert_runbook_url,
        worker_heartbeat_max_age_seconds=settings.alert_worker_heartbeat_max_age_seconds,
        outbox_depth_threshold=settings.alert_outbox_depth_threshold,
    )
    logger.info(
        "alerter_started",
        target=settings.alert_target_base_url,
        poll_interval_seconds=settings.alert_poll_interval_seconds,
    )
    completed = 0
    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, transport=transport) as client:
        while iterations is None or completed < iterations:
            completed += 1
            probe = probe_application(client, settings.alert_target_base_url)
            for message in monitor.evaluate(probe, now=time.monotonic()):
                send_telegram_message(client, settings, message)
            if iterations is None or completed < iterations:
                time.sleep(settings.alert_poll_interval_seconds)


def _run_disabled_loop(iterations: int | None) -> None:
    """Алертинг не сконфигурирован: процесс жив (деплой зелёный), напоминание
    в логах раз в час — неконфигурация видна, но не шумит."""
    completed = 0
    while iterations is None or completed < iterations:
        completed += 1
        logger.warning(
            "alerting_disabled",
            reason="TELEGRAM_ALERT_BOT_TOKEN/TELEGRAM_ALERT_CHAT_ID не заданы",
            runbook="docs/runbooks/alerts.md",
        )
        if iterations is None or completed < iterations:
            time.sleep(_DISABLED_REMINDER_SECONDS)


def main() -> None:  # pragma: no cover — точка входа процесса; логика покрыта run_alerter
    configure_logging(get_settings().log_level)
    run_alerter()


if __name__ == "__main__":  # pragma: no cover
    main()
