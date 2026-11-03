"""Тексты гостей старше обещанного срока — линия алертера (issue #410, ERR-OPS-017).

Политика конфиденциальности обещает гостю: тексты переписки удаляются через
``MESSAGES_RETENTION_DAYS`` (90) дней (spec 0032). Держит обещание ретеншн
воркера, а его маркер ``guest_text_retention_completed`` пишется и при сбое всех
тенантов, и при «удалено 0 строк» (неверный cutoff, контекст тенанта, RLS) — тогда
воркер жив, доставка идёт, и до этой линии не узнавал никто.

Поэтому линия смотрит не на прогон, а на результат: возраст самого старого
сообщения каждого тенанта ``guest_text_oldest_age_seconds`` из ``/metrics``
(считает ``channels/common/retention.py``). Старше срока плюс
``ALERT_GUEST_TEXT_RETENTION_GRACE_HOURS`` — алерт; запас покрывает часовой период
ретеншна и короткий простой воркера, который громко отдельно (ERR-OPS-003).

Алерт состояния, как ERR-OPS-003 и ERR-OPS-008, — по тенанту: одно сообщение на
вход в проблему и ✅ на выход. Тенант без сообщений публикуется нулём — иначе
алерт после починки некому было бы погасить. ``/metrics`` не ответил (None) или
снимок пуст (обновлятор не смог прочитать БД и стёр его) — «не знаю»: линия молчит
и состояния не меняет, недоступная БД уже покрыта ERR-OPS-001.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from hospitality.shared.alerting import format_alert

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_GUEST_TEXT_OVERDUE = "ERR-OPS-017"

_SERIES_PREFIX = "guest_text_oldest_age_seconds{"
_SECONDS_PER_DAY = 86400.0


def read_guest_text_ages(metrics_text: str) -> dict[str, float]:
    """Возраст старейшего сообщения по тенантам из выдачи ``/metrics``; NaN выбрасывается."""
    ages: dict[str, float] = {}
    for line in metrics_text.splitlines():
        if not line.startswith(_SERIES_PREFIX):
            continue
        raw_labels, _, raw_value = line[len(_SERIES_PREFIX) :].rpartition("} ")
        tenant_id = raw_labels.partition('tenant_id="')[2].partition('"')[0]
        value = float(raw_value)
        if tenant_id and not math.isnan(value):
            ages[tenant_id] = value
    return ages


def _days(seconds: float) -> str:
    return f"{seconds / _SECONDS_PER_DAY:.0f} дн"


@dataclass
class GuestTextRetentionLine:
    """Машина линии ERR-OPS-017 — чистая логика, без I/O."""

    retention_days: int
    grace_hours: float
    # Тенанты, о которых уже сказано; размер ограничен числом тенантов.
    alerted: set[str] = field(default_factory=set)

    @property
    def max_age_seconds(self) -> float:
        return self.retention_days * _SECONDS_PER_DAY + self.grace_hours * 3600

    def evaluate(
        self, ages: dict[str, float] | None, *, environment: str, runbook_url: str
    ) -> list[str]:
        """Сообщения о тенантах, перешедших порог в любую сторону с прошлого опроса."""
        if not ages:
            return []
        messages = []
        for tenant_id, age in sorted(ages.items()):
            overdue = age > self.max_age_seconds
            if overdue == (tenant_id in self.alerted):
                continue
            if overdue:
                self.alerted.add(tenant_id)
                title, detail, emoji = self._overdue(age)
            else:
                self.alerted.discard(tenant_id)
                title, detail, emoji = self._recovered(age)
            messages.append(
                format_alert(
                    error_code=ERR_GUEST_TEXT_OVERDUE,
                    title=title,
                    detail=detail,
                    environment=environment,
                    runbook_url=runbook_url,
                    tenant=tenant_id,
                    emoji=emoji,
                )
            )
        return messages

    def _overdue(self, age: float) -> tuple[str, str, str]:
        return (
            f"тексты гостей хранятся дольше обещанных {self.retention_days} дней",
            f"самому старому сообщению {_days(age)} (срок {self.retention_days} дн "
            f"и запас {self.grace_hours:g} ч) — ретеншн не удаляет, и обещание "
            "политики конфиденциальности нарушается с каждым днём. Причина — "
            "guest_text_retention_failed в логе worker или ошибки в Sentry",
            "🔴",
        )

    def _recovered(self, age: float) -> tuple[str, str, str]:
        detail = f"самому старому сообщению {_days(age)}" if age > 0 else "сообщений нет"
        return f"тексты гостей снова моложе {self.retention_days} дней", detail, "✅"
