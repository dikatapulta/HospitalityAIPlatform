"""Персонал упирается в лимит попыток входа — линия алертера (issue #486, ERR-OPS-016).

Отказ 429 «слишком много неудачных попыток» (ERR-AUTH-006) приложение считает
счётчиком ``staff_logins_total{outcome="rate_limited"}``, но до этой линии на него
никто не смотрел: у отеля один внешний адрес на стойку, персонал и гостевой Wi-Fi,
и запертый вход персонала был виден только тем, кто не смог войти.

Линия — всплеск, как ERR-OPS-002: прирост счётчика за опрос не меньше порога
``ALERT_LOGIN_RATE_LIMITED_THRESHOLD`` → алерт, повтор не чаще
``ALERT_COOLDOWN_SECONDS``; ✅ нет — у отказа нет состояния «прошло», окно лимита
кончается само. Порог по умолчанию 1: на пилоте 429 у персонала — редкость, и
каждый — человек, который сейчас не может войти. 0 — линия выключена.

Серия появляется в ``/metrics`` только после первого отказа, поэтому «серии нет»
при ответившем ``/metrics`` — это ноль, а не «не знаю»; ``/metrics`` не ответил —
None, линия молчит и базовую линию не трогает.
"""

from __future__ import annotations

from dataclasses import dataclass

from hospitality.shared.alerting import format_alert

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_STAFF_LOGIN_LOCKOUT = "ERR-OPS-016"

_SERIES_PREFIX = "staff_logins_total{"
_RATE_LIMITED_LABEL = 'outcome="rate_limited"'


def read_login_rate_limited(metrics_text: str) -> float:
    """Счётчик отказов 429 у входа персонала из выдачи ``/metrics``; нет серии — 0."""
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith(_SERIES_PREFIX) and _RATE_LIMITED_LABEL in line:
            total += float(line.rsplit(" ", 1)[-1])
    return total


@dataclass
class LoginLockoutLine:
    """Состояние линии: базовая линия счётчика и время последнего алерта."""

    threshold: int
    cooldown_seconds: float
    last_total: float | None = None
    last_alert_at: float | None = None

    def evaluate(
        self, total: float | None, *, now: float, environment: str, runbook_url: str
    ) -> list[str]:
        """Сообщение, если за опрос прибавилось не меньше порога отказов 429."""
        if total is None or self.threshold <= 0:
            return []
        previous = self.last_total
        self.last_total = total
        if previous is None:
            return []  # первый опрос — только базовая линия
        delta = total - previous
        if delta < 0:
            delta = total  # приложение перезапустилось, счётчик начат с нуля
        if delta < self.threshold:
            return []
        if self.last_alert_at is not None and now - self.last_alert_at < self.cooldown_seconds:
            return []
        self.last_alert_at = now
        return [
            format_alert(
                error_code=ERR_STAFF_LOGIN_LOCKOUT,
                title="персонал упирается в лимит попыток входа в кабинет",
                detail=(
                    f"отказов «слишком много неудачных попыток» с прошлой проверки: {delta:g}. "
                    "Пока окно лимита не пройдёт, эти сотрудники не войдут; чей это логин "
                    "или адрес и что делать — по ссылке ниже"
                ),
                environment=environment,
                runbook_url=runbook_url,
            )
        ]
