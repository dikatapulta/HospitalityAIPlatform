"""«Наставник» не отвечает сотрудникам — линия алертера (issue #911, ERR-OPS-018/019).

До issue #911 у «Наставника» алертов не было (ADR-024 §7 в прежней редакции):
кончился баланс OpenAI, отозван ключ, снята модель или выбран дневной потолок
``LLM_MENTOR_DAILY_BUDGET_USD`` — сотрудник видел «Наставник сейчас не отвечает»,
а основатель узнавал от отеля. С экзаменом всему отелю (#898) это день экзамена,
сорванный молча.

Источник — две серии по журналу вызовов, которые считает ``ai/gateway``
(``outcomes.py``) и публикует ``shared/metrics.py``: ``llm_mentor_failure_streak``
(ошибки и таймауты провайдера после последнего успешного вызова) и
``llm_mentor_budget_rejection_streak`` (отказы потолка подряд в текущих UTC-сутках).
Логика — гостевых линий 5а (ERR-OPS-009/010), тексты — свои: «гостевой бот
молчит» про наставника будил бы не теми словами, и ночью не было бы видно, кто
молчит. Порог серии провайдера — общий с гостем, ``ALERT_LLM_FAILURE_STREAK_THRESHOLD``.

Алерты состояния по тенанту: одно сообщение на вход и ✅ на выход. Тенант без
отказов публикуется нулями — иначе алерт некому было бы погасить. ``/metrics`` не
ответил (None) или снимок стёрт (БД недоступна) — «не знаю»: линия молчит и
состояния не меняет, эти отказы покрыты ERR-OPS-001 и ERR-OPS-007.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from hospitality.shared.alerting import format_alert

# Коды каталога (docs/runbooks/errors.md, R-8).
ERR_MENTOR_PROVIDER_FAILING = "ERR-OPS-018"
ERR_MENTOR_BUDGET_EXHAUSTED = "ERR-OPS-019"

_FAILURE_PREFIX = "llm_mentor_failure_streak{"
_REJECTION_PREFIX = "llm_mentor_budget_rejection_streak{"


@dataclass(frozen=True)
class MentorOutcomes:
    """Серии наставника одного тенанта в снимке ``/metrics``."""

    provider_errors: int
    provider_timeouts: int
    budget_rejections: int


def _series(metrics_text: str, prefix: str) -> list[tuple[str, str, int]]:
    """Строки серии: (тенант, статус, значение); NaN и строки без тенанта выбрасываются."""
    rows = []
    for line in metrics_text.splitlines():
        if not line.startswith(prefix):
            continue
        raw_labels, _, raw_value = line[len(prefix) :].rpartition("} ")
        value = float(raw_value)
        tenant_id = raw_labels.partition('tenant_id="')[2].partition('"')[0]
        status = raw_labels.partition('status="')[2].partition('"')[0]
        if tenant_id and not math.isnan(value):
            rows.append((tenant_id, status, int(value)))
    return rows


def read_mentor_outcomes(metrics_text: str) -> dict[str, MentorOutcomes]:
    """Серии наставника по тенантам из выдачи ``/metrics``.

    Тенант учитывается, только если в снимке есть обе метрики: их публикует вместе
    и вместе стирает ``set_llm_mentor_outcomes``, и половина снимка — «не знаю».
    """
    failures: dict[str, dict[str, int]] = {}
    for tenant_id, status, value in _series(metrics_text, _FAILURE_PREFIX):
        failures.setdefault(tenant_id, {})[status] = value
    rejections = {
        tenant_id: value for tenant_id, _, value in _series(metrics_text, _REJECTION_PREFIX)
    }
    return {
        tenant_id: MentorOutcomes(
            provider_errors=by_status.get("error", 0),
            provider_timeouts=by_status.get("timeout", 0),
            budget_rejections=rejections[tenant_id],
        )
        for tenant_id, by_status in failures.items()
        if tenant_id in rejections
    }


@dataclass
class MentorLine:
    """Машина линий ERR-OPS-018/019 — чистая логика, без I/O."""

    failure_streak_threshold: int
    # Тенанты, о чьём провайдере уже сказано; у потолка — последняя увиденная серия:
    # ✅ приходит, когда она уже ноль, а сказать «сколько отвергнуто» нужно в нём.
    provider_alerted: set[str] = field(default_factory=set)
    budget_exhausted: dict[str, int] = field(default_factory=dict)

    def evaluate(
        self,
        outcomes: dict[str, MentorOutcomes] | None,
        *,
        environment: str,
        runbook_url: str,
    ) -> list[str]:
        """Сообщения о тенантах, чьи серии перешли порог в любую сторону с прошлого опроса."""
        if not outcomes:
            return []
        messages = []
        for tenant_id, tenant_outcomes in sorted(outcomes.items()):
            for code, title, detail, emoji in (
                *self._provider(tenant_id, tenant_outcomes),
                *self._budget(tenant_id, tenant_outcomes),
            ):
                messages.append(
                    format_alert(
                        error_code=code,
                        title=title,
                        detail=detail,
                        environment=environment,
                        runbook_url=runbook_url,
                        tenant=tenant_id,
                        emoji=emoji,
                    )
                )
        return messages

    def _provider(
        self, tenant_id: str, outcomes: MentorOutcomes
    ) -> list[tuple[str, str, str, str]]:
        if self.failure_streak_threshold <= 0:
            return []  # линия выключена страховочным люком, как ERR-OPS-009
        failed = outcomes.provider_errors + outcomes.provider_timeouts
        alerted = tenant_id in self.provider_alerted
        if failed == 0 and alerted:
            self.provider_alerted.discard(tenant_id)
            return [
                (
                    ERR_MENTOR_PROVIDER_FAILING,
                    "«Наставник» снова отвечает",
                    "последний вызов модели наставника успешен",
                    "✅",
                )
            ]
        if alerted or failed < self.failure_streak_threshold:
            return []
        self.provider_alerted.add(tenant_id)
        if outcomes.provider_errors:
            cause = (
                "Чаще всего кончился баланс или отозван ключ у провайдера модели "
                "наставника (LLM_MODEL_MENTOR в .env: gpt-… — OpenAI, claude-… — "
                "Anthropic): Billing и API keys в OpenAI Platform или в Console Anthropic"
            )
        else:
            cause = (
                "Провайдер не успевает ответить — сбой у провайдера модели наставника "
                "или сети сервера: status.openai.com (OpenAI), status.claude.com (Anthropic)"
            )
        return [
            (
                ERR_MENTOR_PROVIDER_FAILING,
                f"«Наставник» не отвечает сотрудникам: {failed} вызова(ов) подряд неуспешны",
                (
                    f"ошибок провайдера: {outcomes.provider_errors}, таймаутов: "
                    f"{outcomes.provider_timeouts}. Сотрудники видят «Наставник сейчас не "
                    "отвечает» — вопросы, тренировка и экзамен стоят; гостевой бот не "
                    f"затронут. {cause}"
                ),
                "🔴",
            )
        ]

    def _budget(self, tenant_id: str, outcomes: MentorOutcomes) -> list[tuple[str, str, str, str]]:
        rejected = outcomes.budget_rejections
        if rejected > 0:
            alerted = tenant_id in self.budget_exhausted
            self.budget_exhausted[tenant_id] = rejected
            if alerted:
                return []
            return [
                (
                    ERR_MENTOR_BUDGET_EXHAUSTED,
                    "дневной потолок «Наставника» выбран, сотрудники без ответа",
                    (
                        f"шлюз отверг вызовов подряд: {rejected} — сотрудники видят "
                        "«Наставник на сегодня ответил на все вопросы, что мог». Гостевой "
                        "бот не затронут. Потолок обнулится в 00:00 UTC; чтобы наставник "
                        "ответил раньше — поднять LLM_MENTOR_DAILY_BUDGET_USD"
                    ),
                    "🔴",
                )
            ]
        last_seen = self.budget_exhausted.pop(tenant_id, None)
        if last_seen is None:
            return []
        return [
            (
                ERR_MENTOR_BUDGET_EXHAUSTED,
                "потолок «Наставника» снова пропускает вызовы",
                (
                    "начались новые UTC-сутки или потолок подняли; за время отказа "
                    f"шлюз отверг вызовов: не меньше {last_seen}"
                ),
                "✅",
            )
        ]
