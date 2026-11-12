"""ЧП и эскалация без адресата — линия 5д алертера (issue #1050, часть 4; ERR-OPS-024).

Бот позвал человека, а звать некого: карточки в кабинете нет (служба эскалаций не
выбрана — у копии до этапа 3 — или карточка не завелась) и Telegram-чата персонала
нет. Гость получил «позвоните на ресепшен» (spec 0051), ход гостя прошёл без ошибки,
в журнале — INFO; отель о госте не знает. Хуже всего — ЧП: гость написал о пожаре
или о плохом самочувствии, и, если он не позвонит сам, о нём не узнает никто.

Источник — счётчик ``escalations_unaddressed_total{tenant_id, reason}``
(``channels/common/escalation_request.py``). Правила чтения — как у ERR-OPS-015
(``alerter_escalation.py``): серия появляется с первым случаем, поэтому отсутствие
тенанта — ноль; рестарт приложения — по спутнику ``_created`` каждой серии.

Поштучный алерт без ✅. ЧП — сразу, на каждом опросе с новым ЧП, без паузы: ЧП редки,
и второе в паузе первого — уже другой гость. Остальные причины — первый случай
сразу, дальше по тенанту не чаще ``ALERT_COOLDOWN_SECONDS``, случаи паузы — числом с
разбивкой по причинам. Первый снимок после старта алертера — базовая линия.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from hospitality.shared.alerting import format_alert
from hospitality.tools.alerter_escalation import FailedEscalations, counter_increase

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_ESCALATION_UNADDRESSED = "ERR-OPS-024"

EMERGENCY_REASON: Final = "emergency"
# Причина эскалации (``ai/escalation.EscalationReason``) — словами для чата команды;
# полноту сверяет тест. Незнакомая причина идёт как есть.
REASON_LABELS: Final[dict[str, str]] = {
    EMERGENCY_REASON: "ЧП",
    "llm_unavailable": "ИИ не ответил",
    "unknown_tool": "бот не смог оформить",
    "tool_execution_failed": "бот не смог оформить",
    "guest_requested_staff": "гость просит сотрудника",
}

_TOTAL_PREFIX = "escalations_unaddressed_total{"
_CREATED_PREFIX = "escalations_unaddressed_created{"

# Серия счётчика: (тенант, причина).
SeriesKey = tuple[str, str]


def _read_series(metrics_text: str, prefix: str) -> dict[SeriesKey, float]:
    values: dict[SeriesKey, float] = {}
    for line in metrics_text.splitlines():
        if not line.startswith(prefix):
            continue
        raw_labels, _, raw_value = line[len(prefix) :].rpartition("} ")
        labels: dict[str, str] = {}
        for pair in raw_labels.split(","):
            key, _, quoted = pair.partition("=")
            labels[key] = quoted.strip('"')
        tenant_id, reason = labels.get("tenant_id", ""), labels.get("reason", "")
        if tenant_id and reason:
            values[(tenant_id, reason)] = float(raw_value)
    return values


def read_unaddressed_escalations(metrics_text: str) -> dict[SeriesKey, FailedEscalations]:
    """Эскалации без адресата по сериям из выдачи ``/metrics``."""
    created = _read_series(metrics_text, _CREATED_PREFIX)
    return {
        key: FailedEscalations(total=total, created=created.get(key))
        for key, total in _read_series(metrics_text, _TOTAL_PREFIX).items()
    }


@dataclass
class UnaddressedEscalationLine:
    """Машина линии ERR-OPS-024 — чистая логика, без I/O."""

    cooldown_seconds: float
    environment: str
    runbook_url: str

    # Последний снимок; None — снимка ещё не было (старт алертера).
    last_seen: dict[SeriesKey, FailedEscalations] | None = None
    # Не названные в канале случаи: тенант → причина → сколько.
    pending: dict[str, dict[str, int]] = field(default_factory=dict)
    # Последнее сообщение не о ЧП — по тенанту (пауза только у них).
    last_alert_at: dict[str, float] = field(default_factory=dict)

    def evaluate(
        self, series: dict[SeriesKey, FailedEscalations] | None, *, now: float
    ) -> list[str]:
        """``series`` None — ``/metrics`` не ответил: «не знаю», состояние прежнее."""
        if series is None:
            return []
        previous, self.last_seen = self.last_seen, dict(series)
        if previous is None:
            return []
        for (tenant_id, reason), counter in series.items():
            added = counter_increase(previous.get((tenant_id, reason)), counter)
            if added > 0:
                by_reason = self.pending.setdefault(tenant_id, {})
                by_reason[reason] = by_reason.get(reason, 0) + added
        messages = []
        for tenant_id in sorted(self.pending):
            by_reason = self.pending[tenant_id]
            emergencies = by_reason.pop(EMERGENCY_REASON, 0)
            if emergencies:
                messages.append(self._emergency(tenant_id, emergencies))
            last = self.last_alert_at.get(tenant_id)
            if by_reason and (last is None or now - last >= self.cooldown_seconds):
                messages.append(self._others(tenant_id, by_reason))
                by_reason.clear()
                self.last_alert_at[tenant_id] = now
        self.pending = {tenant: reasons for tenant, reasons in self.pending.items() if reasons}
        return messages

    def _emergency(self, tenant_id: str, count: int) -> str:
        return format_alert(
            error_code=ERR_ESCALATION_UNADDRESSED,
            title=f"🚨 ЧП без адресата: {count}",
            detail=(
                "гость написал о ЧП, а ни карточки в кабинете, ни Telegram-чата "
                "персонала нет — отель о нём не знает. Бот ответил гостю «позвоните "
                "на ресепшен». Позвоните на ресепшен сами"
            ),
            environment=self.environment,
            runbook_url=self.runbook_url,
            tenant=tenant_id,
        )

    def _others(self, tenant_id: str, by_reason: dict[str, int]) -> str:
        counts: dict[str, int] = {}
        for reason, count in by_reason.items():
            label = REASON_LABELS.get(reason, reason)
            counts[label] = counts.get(label, 0) + count
        causes = ", ".join(f"{label} — {count}" for label, count in sorted(counts.items()))
        cooldown_minutes = round(self.cooldown_seconds / 60)
        return format_alert(
            error_code=ERR_ESCALATION_UNADDRESSED,
            title=f"эскалаций без адресата: {sum(counts.values())}",
            detail=(
                "бот звал сотрудника, а звать некого — ни карточки в кабинете, ни "
                "Telegram-чата персонала; гостям ответили «позвоните на ресепшен». "
                f"Причины: {causes}. Новые случаи — не чаще раза в {cooldown_minutes} мин"
            ),
            environment=self.environment,
            runbook_url=self.runbook_url,
            tenant=tenant_id,
        )
