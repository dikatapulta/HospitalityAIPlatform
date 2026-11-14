"""ИИ в кабинете отказывает сотрудникам — линия алертера (issue #1050, ERR-OPS-021).

Линии ERR-OPS-018/019 смотрят на серию вызовов провайдера по журналу шлюза. Мимо них
проходят отказы, которые сотрудник видит как «Наставник сейчас не отвечает» при ответе
200: исключение кода посреди хода, незаданная модель (ERR-AI-006 — до журнала), провайдер
«через раз» (серия гаснет на первом успехе), незагруженный кодекс. И один отказ без
сообщения вовсе — оценка ответа не разобрана: ответ экзамена не засчитан, сотрудник
получил другой вопрос и об этом не знает.

Источник — счётчик процесса ``mentor_turn_failures_total{tenant_id, flow, reason}``
(``shared/metrics.py``). Правила чтения — как у ERR-OPS-015 (``alerter_escalation.py``):
серия появляется с первым отказом после старта процесса, поэтому «серии нет» при
ответившем ``/metrics`` — ноль; рестарт виден по спутнику ``_created`` или по значению
меньше прошлого — тогда новым считается всё значение; первый снимок после старта
алертера — только базовая линия.

Порог (аудит процесса 11.11, §4): ``ALERT_MENTOR_TURN_FAILURES_THRESHOLD`` отказов
у тенанта за скользящие 10 минут — или хотя бы одна неразобранная оценка. Отказы,
перешедшие порог, не теряются: копятся и приходят числом, первый раз — сразу, дальше
по тенанту не чаще ``ALERT_COOLDOWN_SECONDS``. Алерт поштучный: ✅ нет. 0 — линия
выключена.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field

from hospitality.shared.alerting import format_alert
from hospitality.shared.humanize import plural

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_MENTOR_TURN_FAILURES = "ERR-OPS-021"

WINDOW_SECONDS = 600.0
UNGRADED = "ungraded"

_TOTAL_PREFIX = "mentor_turn_failures_total{"
_CREATED_PREFIX = "mentor_turn_failures_created{"

# Ход (`flow` кабинета) → как он назван в алерте; неизвестный — как есть.
FLOW_NAMES: dict[str, str] = {
    "ask": "«Наставник», «Спросить»",
    "quiz_question": "«Наставник», вопрос «Тренировки»",
    "quiz_check": "«Наставник», проверка «Тренировки»",
    "exam_question": "«Наставник», вопрос экзамена",
    "exam_check": "«Наставник», проверка экзамена",
    "idea_turn": "«Предложить идею»",
    "interview_turn": "«Предложить идею», интервью",
}
# Причина → что случилось и где смотреть.
REASON_NAMES: dict[str, str] = {
    "exception": "ошибка кода (mentor_stream_failed в логе app, событие Sentry)",
    "model_unset": "модель не задана (LLM_MODEL_MENTOR или LLM_MODEL_IDEAS в .env пуст)",
    "provider": "провайдер модели не ответил или ответил ошибкой",
    "codex_missing": "кодекс отеля не загружен",
    UNGRADED: "оценка ответа не разобрана — ответ не засчитан",
}

Key = tuple[str, str, str]  # (тенант, ход, причина)


@dataclass(frozen=True)
class FailureCount:
    """Значение одной серии в снимке ``/metrics``; ``created`` — время её рождения."""

    total: float
    created: float | None = None


def _read_series(metrics_text: str, prefix: str) -> dict[Key, float]:
    values: dict[Key, float] = {}
    for line in metrics_text.splitlines():
        if not line.startswith(prefix):
            continue
        raw_labels, _, raw_value = line[len(prefix) :].rpartition("} ")
        labels = {
            name: raw_labels.partition(f'{name}="')[2].partition('"')[0]
            for name in ("tenant_id", "flow", "reason")
        }
        if labels["tenant_id"]:
            values[labels["tenant_id"], labels["flow"], labels["reason"]] = float(raw_value)
    return values


def read_mentor_turn_failures(metrics_text: str) -> dict[Key, FailureCount]:
    """Отказы хода по сериям (тенант, ход, причина) из выдачи ``/metrics``."""
    created = _read_series(metrics_text, _CREATED_PREFIX)
    return {
        key: FailureCount(total=total, created=created.get(key))
        for key, total in _read_series(metrics_text, _TOTAL_PREFIX).items()
    }


def _added(before: FailureCount | None, now: FailureCount) -> int:
    if before is None:
        return int(now.total)  # серии не было — счёт с нуля
    restarted = now.total < before.total or (
        now.created is not None and before.created is not None and now.created != before.created
    )
    return int(now.total if restarted else now.total - before.total)


@dataclass
class MentorTurnFailureLine:
    """Машина линии ERR-OPS-021 — чистая логика, без I/O.

    ``recent`` — отказы тенанта за скользящее окно: (время, ход, причина, число).
    Окно перешло порог — его отказы уходят в ``owed`` и ждут сообщения, если идёт пауза.
    """

    threshold: int
    cooldown_seconds: float
    last_seen: dict[Key, FailureCount] | None = None
    recent: dict[str, deque[tuple[float, str, str, int]]] = field(default_factory=dict)
    owed: dict[str, Counter[tuple[str, str]]] = field(default_factory=dict)
    last_alert_at: dict[str, float] = field(default_factory=dict)

    def evaluate(
        self,
        failures: dict[Key, FailureCount] | None,
        *,
        now: float,
        environment: str,
        runbook_url: str,
    ) -> list[str]:
        """``failures`` None — ``/metrics`` не ответил: «не знаю», состояние прежнее."""
        if failures is None or self.threshold <= 0:
            return []
        previous, self.last_seen = self.last_seen, dict(failures)
        if previous is None:
            return []
        for key, count in failures.items():
            added = _added(previous.get(key), count)
            if added > 0:
                tenant_id, flow, reason = key
                self.recent.setdefault(tenant_id, deque()).append((now, flow, reason, added))
        for tenant_id in list(self.recent):
            self._settle(tenant_id, now=now)
        messages = []
        for tenant_id, owed in sorted(self.owed.items()):
            last = self.last_alert_at.get(tenant_id)
            if last is not None and now - last < self.cooldown_seconds:
                continue
            self.last_alert_at[tenant_id] = now
            messages.append(self._message(tenant_id, owed, environment, runbook_url))
            del self.owed[tenant_id]
        return messages

    def _settle(self, tenant_id: str, *, now: float) -> None:
        """Убрать отказы старше окна; окно перешло порог — его отказы в долг сообщения."""
        events = self.recent[tenant_id]
        while events and events[0][0] <= now - WINDOW_SECONDS:
            events.popleft()
        total = sum(count for *_, count in events)
        ungraded = any(reason == UNGRADED for _, _, reason, _ in events)
        if total >= self.threshold or ungraded:
            owed = self.owed.setdefault(tenant_id, Counter())
            for _, flow, reason, count in events:
                owed[flow, reason] += count
            events.clear()
        if not events:
            del self.recent[tenant_id]

    def _message(
        self, tenant_id: str, owed: Counter[tuple[str, str]], environment: str, runbook_url: str
    ) -> str:
        total = sum(owed.values())
        parts = [
            f"{FLOW_NAMES.get(flow, flow)} — {REASON_NAMES.get(reason, reason)}: {count}"
            for (flow, reason), count in sorted(owed.items())
        ]
        cooldown_minutes = round(self.cooldown_seconds / 60)
        return format_alert(
            error_code=ERR_MENTOR_TURN_FAILURES,
            title=(
                f"ИИ в кабинете отказывает сотрудникам: {total} "
                f"{plural(total, 'отказ', 'отказа', 'отказов')}"
            ),
            detail=(
                f"{'; '.join(parts)}. Сотрудник видит «…сейчас не отвечает», "
                "а при неразобранной оценке — следующий вопрос, не зная, что ответ не "
                f"засчитан. Новые случаи — не чаще раза в {cooldown_minutes} мин"
            ),
            environment=environment,
            runbook_url=runbook_url,
            tenant=tenant_id,
        )
