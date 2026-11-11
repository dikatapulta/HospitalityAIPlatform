"""Напоминание о незакрытом алерте состояния на копии отеля (issue #1050, часть 3).

Алерт состояния (перечень — шапка ``tools/alerter.py``) говорит один раз на вход
в проблему и ✅ на выход. Пропущенное сообщение — ночью, в суете на стойке —
превращает проблему в тишину: ✅ не приходит, а первое сообщение ушло вверх по
ленте. Поэтому, пока ✅ нет, линия раз в ``ALERT_STATE_REPEAT_SECONDS`` шлёт
короткое напоминание: код, отель, сколько уже без ✅ и ссылка на статью runbook.
Первое сообщение линия не повторяет слово в слово: числа в нём устаревают
(«последний пульс 6 мин назад» через три часа — неправда).

Только на контуре ``pilot`` — метке ``SENTRY_ENVIRONMENT`` гостевой копии (#389).
У staging чат общий, а его проблемы чинят днём по очереди: ежечасный повтор там
стал бы лентой, к которой перестают присматриваться. ERR-OPS-006 не повторяется:
«бюджет почти выбран» — прогноз, а не вред, и висит до конца UTC-суток; вред —
ERR-OPS-010, он повторяется.

Какие алерты открыты, линия узнаёт у машины (``AlertMonitor.active_state_alerts``),
а не разбирает тексты: состояние каждой линии — у неё самой. Отсчёт «без ✅ уже N»
идёт с опроса, на котором алерт открылся; рестарт алертера сбрасывает и алерты,
и отсчёт. Метрика пропала («не знаю») — алерт остаётся открытым и напоминание идёт:
текст говорит ровно «✅ не было», а не «проблема на месте».
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from hospitality.shared.alerting import format_alert
from hospitality.shared.humanize import duration_label_ru

# Метка контура гостевой копии отеля — строка SENTRY_ENVIRONMENT в её .env
# (docs/runbooks/pilot-copy.md, «Метка контура»).
PILOT_ENVIRONMENT: Final = "pilot"


@dataclass(frozen=True)
class ActiveStateAlert:
    """Открытый алерт состояния: код, отель и что сломано — короткой строкой."""

    error_code: str
    tenant: str
    title: str


def state_repeat_seconds(environment: str, configured_seconds: float) -> float:
    """Период напоминаний на этом контуре: настроенный на ``pilot``, иначе 0 (выключено)."""
    return configured_seconds if environment == PILOT_ENVIRONMENT else 0.0


@dataclass
class StateAlertRepeater:
    """Машина напоминаний — чистая логика, без I/O; 0 в периоде выключает линию."""

    interval_seconds: float
    # (код, отель) → (когда алерт открылся, когда о нём сказано последний раз);
    # монотонные секунды, как паузы остальных линий. Размер — число открытых алертов.
    tracked: dict[tuple[str, str], tuple[float, float]] = field(default_factory=dict)

    def evaluate(
        self,
        active: list[ActiveStateAlert],
        *,
        now: float,
        environment: str,
        runbook_url: str,
    ) -> list[str]:
        """Напоминания об алертах, о которых молчали дольше периода.

        Алерт, открывшийся на этом опросе, только заносится в учёт: о нём только
        что сказала его линия. Закрытый (✅ ушло) — из учёта выпадает.
        """
        if self.interval_seconds <= 0:
            return []
        current = {(alert.error_code, alert.tenant): alert for alert in active}
        self.tracked = {key: times for key, times in self.tracked.items() if key in current}
        every = duration_label_ru(int(self.interval_seconds))
        messages = []
        for key in sorted(current):
            opened_at, said_at = self.tracked.setdefault(key, (now, now))
            if now - said_at < self.interval_seconds:
                continue
            self.tracked[key] = (opened_at, now)
            alert = current[key]
            messages.append(
                format_alert(
                    error_code=alert.error_code,
                    title=f"без ✅ уже {duration_label_ru(int(now - opened_at))} — {alert.title}",
                    detail=(
                        "напоминание о незакрытом алерте — сообщения ✅ о восстановлении "
                        f"не было. Следующее — через {every}, если ✅ не придёт"
                    ),
                    environment=environment,
                    runbook_url=runbook_url,
                    tenant=alert.tenant,
                    emoji="🔁",
                )
            )
        return messages
