"""Карточка эскалации не завелась — линия 5б алертера (issue #434, ERR-OPS-015).

У отеля «только кабинет» (spec 0039) карточка эскалации — единственный путь, по
которому персонал узнаёт о госте, которому нужен человек. Шаг карточки
best-effort (spec 0039 §2.6): его отказ не роняет ход гостя, ответ — 200, всплеска
5xx нет, ``/health/ready`` жив. Пишется отказ на WARNING (`escalation_request_skipped`),
Sentry его не видит — громкой остаётся только эта линия, по образцу 5а (ERR-OPS-009).

Источник — счётчик ``escalation_requests_total{outcome="failed"}`` по тенантам
(``shared/metrics.py``). Счётчик живёт в памяти процесса приложения, отсюда два
правила:

- серия появляется в выдаче только с первым отказом после старта процесса.
  Поэтому тенант, которого нет в снимке при ответившем ``/metrics``, — это ноль,
  а не «не знаю»: иначе первый же отказ после каждого рестарта молчал бы как
  базовая линия;
- рестарт обнуляет счётчик. Его видно по спутнику ``escalation_requests_created``
  — времени, когда процесс завёл серию: другое время — счётчик начат заново, и
  новым считается всё его значение. Одного «значение меньше прошлого», как у
  всплеска 5xx (ERR-OPS-002), мало: деплой или откат образа и есть главный повод
  отказа (конфиг разошёлся с кодом), и один отказ до рестарта плюс один после
  дали бы ту же единицу — алерт промолчал бы. Спутника нет в выдаче — остаётся
  правило «меньше прошлого».

Алерт поштучный, а не состояния: ✅ нет, тишина — «новых случаев не было». Первый
случай — сразу, дальше по тенанту не чаще ``ALERT_COOLDOWN_SECONDS``; случаи внутри
паузы не теряются, а копятся и приходят числом в следующем сообщении. Первый снимок
после старта алертера — только базовая линия: прошлые случаи уже названы прежним
процессом алертера или случились до него.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hospitality.shared.alerting import format_alert

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_ESCALATION_CARD_FAILED = "ERR-OPS-015"

_TOTAL_PREFIX = "escalation_requests_total{"
_CREATED_PREFIX = "escalation_requests_created{"
_FAILED_LABEL = 'outcome="failed"'


@dataclass(frozen=True)
class FailedEscalations:
    """Счётчик отказов одного тенанта в снимке ``/metrics``.

    ``created`` — когда процесс приложения завёл серию (Unix-секунды); None —
    спутника в выдаче нет.
    """

    total: float
    created: float | None = None


def _read_failed_series(metrics_text: str, prefix: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for line in metrics_text.splitlines():
        if not line.startswith(prefix) or _FAILED_LABEL not in line:
            continue
        raw_labels, _, raw_value = line[len(prefix) :].rpartition("} ")
        tenant_id = raw_labels.partition('tenant_id="')[2].partition('"')[0]
        if tenant_id:
            values[tenant_id] = float(raw_value)
    return values


def read_escalation_failures(metrics_text: str) -> dict[str, FailedEscalations]:
    """Отказы карточки эскалации по тенантам из выдачи ``/metrics``."""
    created = _read_failed_series(metrics_text, _CREATED_PREFIX)
    return {
        tenant_id: FailedEscalations(total=total, created=created.get(tenant_id))
        for tenant_id, total in _read_failed_series(metrics_text, _TOTAL_PREFIX).items()
    }


def _new_failures(before: FailedEscalations | None, now: FailedEscalations) -> int:
    """Сколько отказов прибавилось с прошлого снимка."""
    if before is None:
        return int(now.total)  # серии не было — счёт с нуля
    restarted = now.total < before.total or (
        now.created is not None and before.created is not None and now.created != before.created
    )
    return int(now.total if restarted else now.total - before.total)


@dataclass
class EscalationFailureLine:
    """Машина линии ERR-OPS-015 — чистая логика, без I/O."""

    cooldown_seconds: float
    environment: str
    runbook_url: str

    # Последний снимок; None — снимка ещё не было (старт алертера).
    last_seen: dict[str, FailedEscalations] | None = None
    # Случаи, ещё не названные в канале, и время последнего сообщения — по тенанту.
    pending: dict[str, int] = field(default_factory=dict)
    last_alert_at: dict[str, float] = field(default_factory=dict)

    def evaluate(self, failures: dict[str, FailedEscalations] | None, *, now: float) -> list[str]:
        """``failures`` None — ``/metrics`` не ответил: «не знаю», состояние прежнее.

        ``now`` — монотонные секунды, как у ``AlertMonitor.evaluate``.
        """
        if failures is None:
            return []
        previous, self.last_seen = self.last_seen, dict(failures)
        if previous is None:
            return []
        for tenant_id, counter in failures.items():
            added = _new_failures(previous.get(tenant_id), counter)
            if added > 0:
                self.pending[tenant_id] = self.pending.get(tenant_id, 0) + added
        messages = []
        for tenant_id, count in sorted(self.pending.items()):
            last = self.last_alert_at.get(tenant_id)
            if last is not None and now - last < self.cooldown_seconds:
                continue
            del self.pending[tenant_id]
            self.last_alert_at[tenant_id] = now
            messages.append(self._message(tenant_id, count))
        return messages

    def _message(self, tenant_id: str, count: int) -> str:
        cooldown_minutes = round(self.cooldown_seconds / 60)
        return format_alert(
            error_code=ERR_ESCALATION_CARD_FAILED,
            title=f"эскалаций без карточки в кабинете: {count}",
            detail=(
                "бот звал сотрудника, а карточка в кабинете не создалась — персонал "
                "этих гостей не видит. Если у отеля нет Telegram-чата персонала, бот "
                "направил гостя на ресепшен. Причина — escalation_request_skipped в "
                f"логе app; новые случаи — не чаще раза в {cooldown_minutes} мин"
            ),
            environment=self.environment,
            runbook_url=self.runbook_url,
            tenant=tenant_id,
        )
