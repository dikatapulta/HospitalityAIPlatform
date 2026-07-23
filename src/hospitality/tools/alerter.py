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

Отправка — прямой ``sendMessage`` Telegram Bot API: канал гостевого бота
(``channels/telegram``) сознательно не переиспользуется — тракт алертов
обязан работать, даже когда код каналов сломан. Состояние — в памяти
(перезапуск = худший случай один повторный алерт); БД и Redis не нужны.

Оба ``TELEGRAM_ALERT_*`` пустые — алертер пассивен (WARNING раз в час):
деплой без настроенных алертов остаётся зелёным, неконфигурация видна в
логах. Заполнен только один — ошибка конфигурации, немедленное падение.

Известное ограничение (docs/runbooks/alerts.md): алертер живёт на том же
VPS — смерть всего сервера не заалертит; лечится внешним uptime-сервисом
(managed, §10.12), вне DoD Task 0018.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from hospitality.shared.config import Settings, get_settings
from hospitality.shared.logging import configure_logging, get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_READY_UNAVAILABLE = "ERR-OPS-001"
ERR_ERROR_SPIKE = "ERR-OPS-002"

_HTTP_TIMEOUT_SECONDS = 5.0
_DISABLED_REMINDER_SECONDS = 3600.0


@dataclass(frozen=True)
class ProbeResult:
    """Снимок одного опроса приложения."""

    ready_ok: bool
    ready_detail: str
    # Сумма счётчиков 5xx из /metrics; None — /metrics недоступен.
    server_error_total: float | None


def sum_server_errors(metrics_text: str) -> float:
    """Сумма ``http_requests_total{...status="5xx"...}`` по всем маршрутам."""
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith("http_requests_total{") and 'status="5xx"' in line:
            total += float(line.rsplit(" ", 1)[-1])
    return total


def format_alert(
    *,
    error_code: str,
    title: str,
    detail: str,
    environment: str,
    runbook_url: str,
    emoji: str = "🔴",
) -> str:
    """Текст алерта по §10.8: код, тенант, correlation id, детали, runbook.

    Алерты алертера — платформенного уровня: тенант — ``platform``,
    correlation id у опроса извне отсутствует — ``—`` (след ищется по времени
    и коду в логах, см. runbook).
    """
    anchor = error_code.lower()
    return (
        f"{emoji} [{environment}] {error_code}: {title}\n"
        f"детали: {detail}\n"
        f"tenant: platform · correlation_id: —\n"
        f"runbook: {runbook_url}#{anchor}"
    )


@dataclass
class AlertMonitor:
    """Машина состояний алертов — чистая логика, без I/O (тестируется без сети)."""

    ready_failure_threshold: int
    error_spike_threshold: int
    cooldown_seconds: float
    environment: str
    runbook_url: str

    consecutive_ready_failures: int = 0
    ready_alert_active: bool = False
    last_server_error_total: float | None = None
    last_spike_alert_at: float | None = None

    def evaluate(self, probe: ProbeResult, *, now: float) -> list[str]:
        """Обработать снимок опроса; вернуть сообщения, которые пора отправить.

        ``now`` — монотонные секунды (``time.monotonic()``): cooldown не должен
        зависеть от перевода системных часов.
        """
        return self._evaluate_ready(probe) + self._evaluate_error_spike(probe, now=now)

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


def probe_application(client: httpx.Client, base_url: str) -> ProbeResult:
    """Один опрос приложения: /health/ready и /metrics."""
    try:
        ready_response = client.get(f"{base_url}/health/ready")
        ready_ok = ready_response.status_code == 200
        ready_detail = ready_response.text.strip()
    except httpx.HTTPError as error:  # диагностический путь: сбой — это статус
        ready_ok = False
        ready_detail = f"connection error: {error}"

    server_error_total: float | None
    try:
        metrics_response = client.get(f"{base_url}/metrics")
        if metrics_response.status_code == 200:
            server_error_total = sum_server_errors(metrics_response.text)
        else:
            server_error_total = None
    except httpx.HTTPError:
        server_error_total = None

    return ProbeResult(
        ready_ok=ready_ok, ready_detail=ready_detail, server_error_total=server_error_total
    )


def send_telegram_message(client: httpx.Client, settings: Settings, text: str) -> None:
    """Отправить сообщение в Telegram-канал команды; сбой отправки логируется,
    но не роняет цикл (следующая итерация попробует снова при новом алерте)."""
    url = f"{settings.telegram_api_base_url}/bot{settings.telegram_alert_bot_token}/sendMessage"
    try:
        response = client.post(url, json={"chat_id": settings.telegram_alert_chat_id, "text": text})
        response.raise_for_status()
        logger.info("alert_sent", text=text)
    except httpx.HTTPError:
        logger.error("alert_send_failed", text=text, exc_info=True)


def run_alerter(
    iterations: int | None = None, *, transport: httpx.BaseTransport | None = None
) -> None:
    """Цикл алертера. ``iterations``/``transport`` — только для тестов."""
    settings = get_settings()
    configured = [bool(settings.telegram_alert_bot_token), bool(settings.telegram_alert_chat_id)]
    if any(configured) and not all(configured):
        # Fail-fast (§11): полузаполненная пара — не «тихо выключено», а ошибка.
        raise SystemExit(
            "TELEGRAM_ALERT_BOT_TOKEN и TELEGRAM_ALERT_CHAT_ID задаются только парой "
            "(docs/runbooks/alerts.md); заполнен ровно один — исправьте .env"
        )
    if not all(configured):
        _run_disabled_loop(iterations)
        return

    monitor = AlertMonitor(
        ready_failure_threshold=settings.alert_ready_failure_threshold,
        error_spike_threshold=settings.alert_error_spike_threshold,
        cooldown_seconds=settings.alert_cooldown_seconds,
        environment=settings.sentry_environment,
        runbook_url=settings.alert_runbook_url,
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
