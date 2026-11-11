"""Вход персонала — две линии алертера: упор в лимит попыток (issue #486, ERR-OPS-016)
и подбор пароля ниже лимита (issue #967, ERR-OPS-020).

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

Подбор (ERR-OPS-020) лимит не видит: девять неверных паролей на учётку за окно
лимита или один пароль по всем учёткам — ни одного 429 (аудит 10.11, находка В1-3).
Линия считает ``staff_logins_total{outcome="rejected"}`` — неверный логин или
пароль, — но не за опрос, а за скользящий час: подбор под лимитом идёт медленно,
по попытке в минуту, и прирост одного опроса его не отличит от опечатки. Порог
``ALERT_LOGIN_REJECTED_HOURLY_THRESHOLD`` отказов за час → алерт, повтор не чаще
раза в час; 0 — линия выключена.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from hospitality.shared.alerting import format_alert

# Коды каталога (docs/runbooks/errors.md, R-8).
ERR_STAFF_LOGIN_LOCKOUT = "ERR-OPS-016"
ERR_STAFF_LOGIN_GUESSING = "ERR-OPS-020"

_SERIES_PREFIX = "staff_logins_total{"
_RATE_LIMITED_LABEL = 'outcome="rate_limited"'
_REJECTED_LABEL = 'outcome="rejected"'

# Окно линии подбора: час — десятки попыток подбора под лимитом и ещё мало опечаток смены.
GUESSING_WINDOW_SECONDS = 3600.0


def _read_outcome(metrics_text: str, label: str) -> float:
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith(_SERIES_PREFIX) and label in line:
            total += float(line.rsplit(" ", 1)[-1])
    return total


def read_login_rate_limited(metrics_text: str) -> float:
    """Счётчик отказов 429 у входа персонала из выдачи ``/metrics``; нет серии — 0."""
    return _read_outcome(metrics_text, _RATE_LIMITED_LABEL)


def read_login_rejected(metrics_text: str) -> float:
    """Счётчик неверных логинов и паролей у входа персонала; нет серии — 0."""
    return _read_outcome(metrics_text, _REJECTED_LABEL)


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


@dataclass
class LoginGuessingLine:
    """Линия ERR-OPS-020: отказы входа за скользящий час.

    ``samples`` — (время опроса, счётчик); первый элемент — база окна: последний
    опрос не позже начала часа. Опрос раз в минуту — около 60 элементов.
    """

    threshold: int
    samples: deque[tuple[float, float]] = field(default_factory=deque)
    last_alert_at: float | None = None

    def evaluate(
        self, total: float | None, *, now: float, environment: str, runbook_url: str
    ) -> list[str]:
        """Сообщение, если за последний час отказов входа не меньше порога."""
        if total is None or self.threshold <= 0:
            return []
        if self.samples and total < self.samples[-1][1]:
            # Приложение перезапустилось: счётчик начат с нуля, прежние опросы не база.
            self.samples.clear()
            self.samples.append((now, 0.0))
        self.samples.append((now, total))
        window_start = now - GUESSING_WINDOW_SECONDS
        while len(self.samples) > 1 and self.samples[1][0] <= window_start:
            self.samples.popleft()
        rejected = total - self.samples[0][1]
        if rejected < self.threshold:
            return []
        if self.last_alert_at is not None and now - self.last_alert_at < GUESSING_WINDOW_SECONDS:
            return []
        self.last_alert_at = now
        return [
            format_alert(
                error_code=ERR_STAFF_LOGIN_GUESSING,
                title="много неверных паролей у входа в кабинет персонала",
                detail=(
                    f"за последний час неверных логинов или паролей: {rejected:g} "
                    f"(порог {self.threshold}). Так выглядит подбор пароля к учёткам "
                    "персонала; в день раздачи логинов это могут быть опечатки. "
                    "Что смотреть — по ссылке ниже"
                ),
                environment=environment,
                runbook_url=runbook_url,
            )
        ]
