"""Заявку не взяли за срок — линия 10а алертера (issue #1050, часть 4; ERR-OPS-025).

Сигнал кабинета зовёт персонал только из открытой вкладки (spec 0039 §3.4): закрыли
её или браузер её усыпил — невзятая заявка ждёт молча, а у отеля «только кабинет»
другого пути к персоналу нет. Линия говорит об этом команде: по тенанту число
невзятых заявок дольше срока команды и возраст самой старой из них —
``service_requests_forgotten{kind}`` и ``service_requests_forgotten_oldest_age_seconds``
из ``/metrics`` (считает ``staff_portal/unclaimed_watch.py``; сроки — spec 0018 §3,
п. 8а).

Алерт состояния по тенанту, как ERR-OPS-017: одно сообщение на вход и ✅, когда
таких заявок не осталось; на контуре ``pilot`` — ещё и напоминание раз в час
(``alerter_repeat.py``). Только на ``pilot``: у staging демо-отель держит
невзятые заявки нарочно, а алертер перезапускается каждым деплоем — линия
повторяла бы одно и то же в общий чат десятки раз в сутки. ``/metrics`` не ответил
или снимок пуст (обновлятор не прочитал БД) — «не знаю»: линия молчит.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from hospitality.shared.alerting import format_alert
from hospitality.shared.humanize import duration_label_ru
from hospitality.tools.alerter_repeat import PILOT_ENVIRONMENT

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_REQUESTS_UNCLAIMED = "ERR-OPS-025"

_COUNT_PREFIX = "service_requests_forgotten{"
_AGE_PREFIX = "service_requests_forgotten_oldest_age_seconds{"


@dataclass(frozen=True)
class ForgottenRequests:
    """Снимок одного тенанта — пара ``shared.metrics.ForgottenRequests``.

    Своя копия, а не импорт: алертер не тянет код приложения (spec 0018 §3 — тракт
    алертов работает, даже когда сломано приложение); поля сверяет тест.
    """

    urgent: int
    regular: int
    oldest_age_seconds: float


def _tenant_of(raw_labels: str) -> str:
    return raw_labels.partition('tenant_id="')[2].partition('"')[0]


def read_forgotten_requests(metrics_text: str) -> dict[str, ForgottenRequests]:
    """Забытые заявки по тенантам; тенант без любой из трёх серий не учитывается."""
    counts: dict[str, dict[str, int]] = {}
    ages: dict[str, float] = {}
    for line in metrics_text.splitlines():
        for prefix in (_COUNT_PREFIX, _AGE_PREFIX):
            if not line.startswith(prefix):
                continue
            raw_labels, _, raw_value = line[len(prefix) :].rpartition("} ")
            tenant_id, value = _tenant_of(raw_labels), float(raw_value)
            if not tenant_id or math.isnan(value):
                continue
            if prefix is _AGE_PREFIX:
                ages[tenant_id] = value
            else:
                kind = raw_labels.partition('kind="')[2].partition('"')[0]
                counts.setdefault(tenant_id, {})[kind] = int(value)
    return {
        tenant_id: ForgottenRequests(
            urgent=by_kind["urgent"], regular=by_kind["regular"], oldest_age_seconds=ages[tenant_id]
        )
        for tenant_id, by_kind in counts.items()
        if {"urgent", "regular"} <= by_kind.keys() and tenant_id in ages
    }


def unclaimed_line_enabled(environment: str) -> bool:
    """Линия включена только на контуре копии отеля."""
    return environment == PILOT_ENVIRONMENT


@dataclass
class UnclaimedRequestLine:
    """Машина линии ERR-OPS-025 — чистая логика, без I/O."""

    enabled: bool
    # Тенанты, о которых уже сказано; размер ограничен числом тенантов.
    alerted: set[str] = field(default_factory=set)

    def evaluate(
        self,
        snapshot: dict[str, ForgottenRequests] | None,
        *,
        environment: str,
        runbook_url: str,
    ) -> list[str]:
        """Сообщения о тенантах, перешедших порог в любую сторону с прошлого опроса."""
        if not self.enabled or not snapshot:
            return []
        messages = []
        for tenant_id, forgotten in sorted(snapshot.items()):
            total = forgotten.urgent + forgotten.regular
            if (total > 0) == (tenant_id in self.alerted):
                continue
            if total > 0:
                self.alerted.add(tenant_id)
                title, detail, emoji = self._forgotten(forgotten, total)
            else:
                self.alerted.discard(tenant_id)
                title, detail, emoji = (
                    "заявок дольше срока больше нет",
                    "все они взяты или закрыты",
                    "✅",
                )
            messages.append(
                format_alert(
                    error_code=ERR_REQUESTS_UNCLAIMED,
                    title=title,
                    detail=detail,
                    environment=environment,
                    runbook_url=runbook_url,
                    tenant=tenant_id,
                    emoji=emoji,
                )
            )
        return messages

    @staticmethod
    def _forgotten(forgotten: ForgottenRequests, total: int) -> tuple[str, str, str]:
        urgent = f" (срочных и от бота: {forgotten.urgent})" if forgotten.urgent else ""
        return (
            f"заявки в кабинете ждут дольше срока: {total}{urgent}",
            f"самая старая ждёт {duration_label_ru(int(forgotten.oldest_age_seconds))}. "
            "Кабинет уже звал ресепшен и менеджера — похоже, вкладка на стойке закрыта "
            "или уснула. Позвоните на ресепшен",
            "🔴",
        )
