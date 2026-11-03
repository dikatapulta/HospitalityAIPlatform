"""Алертер не видит ``/metrics`` — линия алертера (issue #264, ERR-OPS-007).

Почти все линии алертера читают ``/metrics``: всплеск 5xx, пульс воркера, очередь
событий, бюджет и отказы модели, эскалации, лимиты входа, возраст текстов гостей.
Не ответил ``/metrics`` — каждая из них получает «не знаю» и молчит: ноль был бы
ложью. Когда лежит всё приложение, это верно — о нём говорит ERR-OPS-001. Но при
частном отказе ``/health/ready`` отвечает 200, а ``/metrics`` нет (обратный прокси,
таймаут медленного скрейпа, регрессия в роутере метрик) — и тогда алертер зелен
и нем, а мёртвый воркер не видит никто.

Линия — состояния, симметрично ERR-OPS-001: ``/metrics`` не ответил или ответил
не-200 столько опросов подряд, сколько ``ALERT_READY_FAILURE_THRESHOLD``, → одно
сообщение на вход и ✅ на выход. Отдельного порога нет: сбой того же рода, что
у ready, и того же веса.

Считаются только опросы, где ``/health/ready`` ответил 200. Не ответили оба —
лежит приложение целиком, это ERR-OPS-001; счёт тогда не растёт и не сбрасывается,
чтобы падение всего приложения не давало второго алерта о том же.
"""

from __future__ import annotations

from dataclasses import dataclass

from hospitality.shared.alerting import format_alert

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_METRICS_UNAVAILABLE = "ERR-OPS-007"

# Линии, которые молчат без /metrics, — для текста алерта; новая линия из метрик
# дописывает себя сюда.
_BLIND_LINES = (
    "мёртвый воркер, растущая очередь, всплеск 5xx, бюджет и отказы модели, "
    "эскалации, лимиты входа, срок хранения текстов гостей"
)


@dataclass
class MetricsScrapeLine:
    """Машина линии ERR-OPS-007 — чистая логика, без I/O."""

    failure_threshold: int
    consecutive_failures: int = 0
    alert_active: bool = False

    def evaluate(
        self,
        *,
        ready_ok: bool,
        metrics_ok: bool,
        metrics_detail: str,
        environment: str,
        runbook_url: str,
    ) -> list[str]:
        """Сообщение на N-й подряд отказ ``/metrics`` при живом ready и ✅ на выход."""
        if metrics_ok:
            self.consecutive_failures = 0
            if not self.alert_active:
                return []
            self.alert_active = False
            return [
                format_alert(
                    error_code=ERR_METRICS_UNAVAILABLE,
                    title="алертер снова видит /metrics",
                    detail="алерты из метрик снова работают",
                    environment=environment,
                    runbook_url=runbook_url,
                    emoji="✅",
                )
            ]
        if not ready_ok:
            return []  # лежит всё приложение — это ERR-OPS-001
        self.consecutive_failures += 1
        if self.alert_active or self.consecutive_failures < self.failure_threshold:
            return []
        self.alert_active = True
        return [
            format_alert(
                error_code=ERR_METRICS_UNAVAILABLE,
                title=f"алертер не видит /metrics {self.consecutive_failures} опроса(ов) подряд",
                detail=(
                    f"{metrics_detail}; /health/ready при этом отвечает. Пока /metrics "
                    f"не виден, молчат алерты из метрик: {_BLIND_LINES}"
                ),
                environment=environment,
                runbook_url=runbook_url,
            )
        ]
