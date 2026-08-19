"""Алертер: watchdog staging → Telegram-канал команды (Task 0018, §10.8).

Четвёртый процесс staging-стека — тот же образ приложения, другая команда
(канон «один образ, другая точка входа», §5.3, как ``hospitality.worker``):

    python -m hospitality.tools.alerter

Цикл раз в ``ALERT_POLL_INTERVAL_SECONDS``:

1. ``GET /health/ready``. Не-200 или сетевая ошибка
   ``ALERT_READY_FAILURE_THRESHOLD`` опросов подряд → алерт **ERR-OPS-001**
   (однократно, до восстановления); первый успех после алерта → сообщение
   о восстановлении.
2. ``GET /metrics``: прирост суммы ``http_requests_total{status="5xx"}`` за
   интервал ≥ ``ALERT_ERROR_SPIKE_THRESHOLD`` → алерт **ERR-OPS-002**, не чаще
   ``ALERT_COOLDOWN_SECONDS``. Недоступный ``/metrics`` — пропуск шага:
   падение приложения целиком уже покрыто ERR-OPS-001.
3. Оттуда же ``worker_heartbeat_age_seconds`` старше
   ``ALERT_WORKER_HEARTBEAT_MAX_AGE_SECONDS`` → алерт **ERR-OPS-003**: воркер
   мёртв или завис, и вся доставка событий стоит молча (issue #136). Проверяет
   именно watchdog, а не сам воркер: о своей смерти процесс не докладывает.
4. И ``outbox_pending_events`` выше ``ALERT_OUTBOX_DEPTH_THRESHOLD`` → алерт
   **ERR-OPS-004**. Вторая линия на тот же симптом: она ловит случай, которого
   пульс не видит, — цикл жив и пульс свежий, но очередь не разбирается
   (доставки падают по кругу, воркер не успевает).
5. Оттуда же пара ``llm_daily_spend_usd`` / ``llm_daily_budget_usd`` по каждому
   тенанту: расход дошёл до ``ALERT_LLM_BUDGET_RATIO`` от лимита → алерт
   **ERR-OPS-006** (issue #103). Дальше по этой прямой — ERR-AI-002: gateway
   начинает отвергать вызовы, и бот молча перестаёт отвечать гостям до конца
   UTC-суток. Состояние — по тенанту: у каждого отеля свой бюджет и свой день.
6. И единственный шаг не по HTTP: возраст свежайшего ночного бэкапа в каталоге
   ``ALERT_BACKUP_DIR`` (в staging-стеке — каталог хоста, смонтированный только
   для чтения). Старше ``ALERT_BACKUP_MAX_AGE_HOURS``, каталог пуст или не
   читается → алерт **ERR-OPS-008** (issue #106): с fail-closed шифрованием
   (issue #81) бэкап не создаётся вовсе, если на сервере нет ключа age или
   самого бинарника, и до issue #106 узнать об этом было неоткуда до дня аварии.

Алерты состояния (1, 3, 4, 5, 6) устроены одинаково: одно сообщение на вход в
проблему и ✅ на выход, а не лента каждую минуту. Пустая метрика (NaN — БД
недоступна, или /metrics не ответил) — это «не знаю», а не «всё хорошо»:
шаг молча пропускается, потому что такой отказ уже покрыт ERR-OPS-001.
У шестого шага правило обратное — «не знаю» там тоже алерт: нечитаемый каталог
бэкапов не покрыт ничем, и это ровно та тишина, ради которой заведена #106.

Формат текста и отправка — общий канон ``shared/alerting.py`` (им же
докладывает воркер о dead-letter, issue #133); состояние — в памяти
(перезапуск = худший случай один повторный алерт), БД и Redis не нужны.

Оба ``TELEGRAM_ALERT_*`` пустые — алертер пассивен (WARNING раз в час):
деплой без настроенных алертов остаётся зелёным, неконфигурация видна в
логах. Заполнен только один — ошибка конфигурации, немедленное падение.

Известное ограничение (docs/runbooks/alerts.md): алертер живёт на том же
VPS — смерть всего сервера не заалертит; лечится внешним uptime-сервисом
(managed, §10.12), вне DoD Task 0018.
"""

from __future__ import annotations

import math
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from fnmatch import fnmatch

import httpx

from hospitality.shared.alerting import alert_request, alerting_configured, format_alert
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.logging import configure_logging, get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_READY_UNAVAILABLE = "ERR-OPS-001"
ERR_ERROR_SPIKE = "ERR-OPS-002"
ERR_WORKER_STALLED = "ERR-OPS-003"
ERR_OUTBOX_BACKLOG = "ERR-OPS-004"
ERR_LLM_BUDGET_NEAR_LIMIT = "ERR-OPS-006"
ERR_BACKUP_STALE = "ERR-OPS-008"

_HTTP_TIMEOUT_SECONDS = 5.0
_DISABLED_REMINDER_SECONDS = 3600.0

# Имена файлов ночного бэкапа; формат задаёт `ops/backup/backup.sh` (метка по
# умолчанию — `hospitality`), он же владелец этого факта. Маска намеренно узкая:
# в том же каталоге лежат снимки перед миграцией `pre-migrate-*` (issue #135), и
# считать их бэкапом нельзя — тогда умерший ночной cron маскировался бы снимками
# деплоя (решение PR #284, docs/runbooks/restore.md), то есть ровно той тишиной,
# ради которой заведена issue #106.
_BACKUP_FILE_GLOB = "hospitality-*.dump.age"


@dataclass(frozen=True)
class TenantBudgetUsage:
    """Расход и лимит LLM одного тенанта за текущие UTC-сутки (issue #103)."""

    tenant_id: str
    spent_usd: float
    budget_usd: float


@dataclass(frozen=True)
class BackupProbe:
    """Что видно в каталоге ночных бэкапов на момент опроса (issue #106).

    ``readable=False`` — каталога нет или он не открывается (причина от ОС —
    в ``error``). ``latest_age_seconds=None`` при ``readable=True`` — каталог
    открылся, но подходящих файлов в нём нет.
    """

    directory: str
    readable: bool
    latest_age_seconds: float | None
    error: str = ""


@dataclass(frozen=True)
class ProbeResult:
    """Снимок одного опроса приложения.

    Скалярные значения из /metrics — `None`, когда их нет: эндпоинт не ответил
    или метрика пуста (NaN, БД недоступна). «Нет данных» ≠ «ноль».
    """

    ready_ok: bool
    ready_detail: str
    # Сумма счётчиков 5xx из /metrics; None — /metrics недоступен.
    server_error_total: float | None
    # Возраст пульса воркера и глубина очереди outbox (issue #136).
    # Без умолчания `= None` намеренно: «нет данных» — это молчание алерта, и
    # забытое поле выключило бы линию тихо. Теперь оно ошибка mypy.
    worker_heartbeat_age_seconds: float | None
    outbox_pending_events: float | None
    # Расход к лимиту по тенантам (issue #103). Пустой список — «не знаю»
    # (метрик нет: /metrics не ответил, БД недоступна, версия приложения
    # старше): по тем же правилам, что None выше, шаг молчит. Тенант, который
    # сегодня не потратил ничего, в списке ПРИСУТСТВУЕТ с нулём — иначе алерт
    # о вчерашнем расходе некому было бы погасить.
    llm_budget_usage: list[TenantBudgetUsage]
    # Каталог бэкапов (issue #106); None — линия выключена (ALERT_BACKUP_DIR
    # пуст: dev, CI). Здесь, в отличие от полей выше, None НЕ значит «не знаю»:
    # «не знаю» про бэкап — это BackupProbe с readable=False, и он алертит.
    backup: BackupProbe | None


def sum_server_errors(metrics_text: str) -> float:
    """Сумма ``http_requests_total{...status="5xx"...}`` по всем маршрутам."""
    total = 0.0
    for line in metrics_text.splitlines():
        if line.startswith("http_requests_total{") and 'status="5xx"' in line:
            total += float(line.rsplit(" ", 1)[-1])
    return total


def read_gauge(metrics_text: str, name: str) -> float | None:
    """Значение gauge без лейблов из выдачи ``/metrics``; None — нет значения.

    None отдаётся и когда метрики в тексте нет (старая версия приложения), и
    когда она NaN (``shared/metrics.py`` пишет так «не знаю»: БД недоступна).
    Оба случая для алертов одинаковы — молчать, а не считать нулём.
    """
    prefix = f"{name} "
    for line in metrics_text.splitlines():
        if line.startswith(prefix):
            value = float(line[len(prefix) :])
            return None if math.isnan(value) else value
    return None


def read_gauge_by_tenant(metrics_text: str, name: str) -> dict[str, float]:
    """Значения gauge с единственным лейблом ``tenant_id``: тенант → значение.

    Строка выдачи выглядит как ``llm_daily_spend_usd{tenant_id="…"} 8.24``.
    NaN-значения выбрасываются по тому же правилу, что в ``read_gauge``:
    «не знаю» не должно превращаться в число.
    """
    values: dict[str, float] = {}
    prefix = f"{name}{{"
    for line in metrics_text.splitlines():
        if not line.startswith(prefix):
            continue
        labels, _, raw_value = line[len(prefix) :].rpartition("} ")
        tenant_id = labels.partition('tenant_id="')[2].partition('"')[0]
        if not tenant_id:
            continue
        value = float(raw_value)
        if not math.isnan(value):
            values[tenant_id] = value
    return values


def read_llm_budget_usage(metrics_text: str) -> list[TenantBudgetUsage]:
    """Пары «расход/лимит» по тенантам из выдачи ``/metrics`` (issue #103).

    Тенант учитывается, только если в снимке есть ОБА числа: доля от лимита
    имеет смысл лишь тогда, когда они из одного снимка (``set_llm_daily_budget``
    публикует их вместе и вместе же стирает).
    """
    spend = read_gauge_by_tenant(metrics_text, "llm_daily_spend_usd")
    budget = read_gauge_by_tenant(metrics_text, "llm_daily_budget_usd")
    return [
        TenantBudgetUsage(tenant_id=tenant_id, spent_usd=spent_usd, budget_usd=budget[tenant_id])
        for tenant_id, spent_usd in spend.items()
        if tenant_id in budget
    ]


def _humanize_seconds(seconds: float) -> str:
    """«42 сек» / «7 мин» / «31 ч» — возраст и порог в тексте, который читает человек.

    Часы появились с issue #106: там пороги суточного порядка, и «1800 мин»
    человек в три часа ночи не читает.
    """
    if seconds < 90:
        return f"{int(seconds)} сек"
    if seconds < 5400:  # полтора часа
        return f"{round(seconds / 60)} мин"
    return f"{round(seconds / 3600)} ч"


@dataclass
class AlertMonitor:
    """Машина состояний алертов — чистая логика, без I/O (тестируется без сети)."""

    ready_failure_threshold: int
    error_spike_threshold: int
    cooldown_seconds: float
    environment: str
    runbook_url: str
    worker_heartbeat_max_age_seconds: float
    outbox_depth_threshold: int
    llm_budget_ratio: float
    backup_max_age_seconds: float

    consecutive_ready_failures: int = 0
    ready_alert_active: bool = False
    last_server_error_total: float | None = None
    last_spike_alert_at: float | None = None
    heartbeat_alert_active: bool = False
    outbox_depth_alert_active: bool = False
    # Тенанты, о чьём бюджете уже сказано (issue #103): состояние по тенанту,
    # потому что сутки и лимит у каждого отеля свои. Множество не растёт без
    # предела — его размер ограничен числом тенантов инсталляции.
    llm_budget_alerted_tenants: set[str] = field(default_factory=set)
    backup_alert_active: bool = False

    def evaluate(self, probe: ProbeResult, *, now: float) -> list[str]:
        """Обработать снимок опроса; вернуть сообщения, которые пора отправить.

        ``now`` — монотонные секунды (``time.monotonic()``): cooldown не должен
        зависеть от перевода системных часов.
        """
        return (
            self._evaluate_ready(probe)
            + self._evaluate_error_spike(probe, now=now)
            + self._evaluate_worker_heartbeat(probe)
            + self._evaluate_outbox_depth(probe)
            + self._evaluate_llm_budget(probe)
            + self._evaluate_backup_freshness(probe)
        )

    def _evaluate_ready(self, probe: ProbeResult) -> list[str]:
        if probe.ready_ok:
            recovered = self.ready_alert_active
            self.ready_alert_active = False
            self.consecutive_ready_failures = 0
            if recovered:
                return [
                    format_alert(
                        error_code=ERR_READY_UNAVAILABLE,
                        title="/health/ready снова отвечает",
                        detail=probe.ready_detail,
                        environment=self.environment,
                        runbook_url=self.runbook_url,
                        emoji="✅",
                    )
                ]
            return []
        self.consecutive_ready_failures += 1
        if (
            self.ready_alert_active
            or self.consecutive_ready_failures < self.ready_failure_threshold
        ):
            return []
        self.ready_alert_active = True
        return [
            format_alert(
                error_code=ERR_READY_UNAVAILABLE,
                title=(
                    f"/health/ready недоступен или нездоров "
                    f"{self.consecutive_ready_failures} опроса(ов) подряд"
                ),
                detail=probe.ready_detail,
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]

    def _evaluate_error_spike(self, probe: ProbeResult, *, now: float) -> list[str]:
        if probe.server_error_total is None:
            return []
        previous_total = self.last_server_error_total
        self.last_server_error_total = probe.server_error_total
        if previous_total is None:
            return []  # первый опрос — только базовая линия
        delta = probe.server_error_total - previous_total
        if delta < 0:
            # Счётчик обнулился (процесс приложения перезапустился) —
            # всплеском считаем накопленное с нуля.
            delta = probe.server_error_total
        if delta < self.error_spike_threshold:
            return []
        if (
            self.last_spike_alert_at is not None
            and now - self.last_spike_alert_at < self.cooldown_seconds
        ):
            return []
        self.last_spike_alert_at = now
        return [
            format_alert(
                error_code=ERR_ERROR_SPIKE,
                title="всплеск ошибок 5xx",
                detail=f"+{delta:g} ответов 5xx за интервал опроса",
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]

    def _evaluate_worker_heartbeat(self, probe: ProbeResult) -> list[str]:
        """Пульс воркера старше порога — ERR-OPS-003 (issue #136).

        Алерт состояния, как ERR-OPS-001: одно сообщение на вход в проблему и
        ✅ на выход. «Возраста нет» (None) — молчим: это либо недоступная БД
        (уже ERR-OPS-001), либо приложение старой версии без метрики.
        """
        age = probe.worker_heartbeat_age_seconds
        if age is None:
            return []
        if age <= self.worker_heartbeat_max_age_seconds:
            if not self.heartbeat_alert_active:
                return []
            self.heartbeat_alert_active = False
            return [
                format_alert(
                    error_code=ERR_WORKER_STALLED,
                    title="воркер событий снова подаёт признаки жизни",
                    detail=f"последний пульс {_humanize_seconds(age)} назад",
                    environment=self.environment,
                    runbook_url=self.runbook_url,
                    emoji="✅",
                )
            ]
        if self.heartbeat_alert_active:
            return []
        self.heartbeat_alert_active = True
        threshold = _humanize_seconds(self.worker_heartbeat_max_age_seconds)
        return [
            format_alert(
                error_code=ERR_WORKER_STALLED,
                title="воркер событий не подаёт признаков жизни",
                detail=(
                    f"последний пульс {_humanize_seconds(age)} назад (порог {threshold}) — "
                    "доставка событий стоит: уведомления службам, напоминания "
                    "и эскалации не идут"
                ),
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]

    def _evaluate_outbox_depth(self, probe: ProbeResult) -> list[str]:
        """Очередь outbox выше порога — ERR-OPS-004 (вторая линия, issue #136)."""
        depth = probe.outbox_pending_events
        if depth is None:
            return []
        if depth <= self.outbox_depth_threshold:
            if not self.outbox_depth_alert_active:
                return []
            self.outbox_depth_alert_active = False
            return [
                format_alert(
                    error_code=ERR_OUTBOX_BACKLOG,
                    title="очередь событий вернулась в норму",
                    detail=f"сейчас в очереди {depth:g} (порог {self.outbox_depth_threshold})",
                    environment=self.environment,
                    runbook_url=self.runbook_url,
                    emoji="✅",
                )
            ]
        if self.outbox_depth_alert_active:
            return []
        self.outbox_depth_alert_active = True
        return [
            format_alert(
                error_code=ERR_OUTBOX_BACKLOG,
                title="очередь событий растёт",
                detail=(
                    f"сейчас в очереди {depth:g} "
                    f"(порог {self.outbox_depth_threshold}) — "
                    "воркер стоит или не успевает"
                ),
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]

    def _evaluate_llm_budget(self, probe: ProbeResult) -> list[str]:
        """Расход тенанта дошёл до доли лимита — ERR-OPS-006 (issue #103).

        Алерт состояния, как ERR-OPS-003/004, но своего на каждый тенант: одно
        сообщение на переход через порог и ✅ на возврат ниже (смена UTC-суток
        или поднятый лимит). Тенант, которого в снимке нет вовсе, состояние не
        меняет: это «не знаю», а не «расход упал».
        """
        if not 0.0 < self.llm_budget_ratio < 1.0:
            return []  # линия выключена страховочным люком
        messages = []
        for usage in probe.llm_budget_usage:
            if usage.budget_usd <= 0:
                continue  # лимит не задан — сравнивать не с чем
            share = usage.spent_usd / usage.budget_usd
            spent = f"${usage.spent_usd:.2f} из ${usage.budget_usd:.2f}"
            if share < self.llm_budget_ratio:
                if usage.tenant_id not in self.llm_budget_alerted_tenants:
                    continue
                self.llm_budget_alerted_tenants.discard(usage.tenant_id)
                messages.append(
                    format_alert(
                        error_code=ERR_LLM_BUDGET_NEAR_LIMIT,
                        title="расход LLM снова ниже порога",
                        detail=(
                            f"{spent} ({share:.0%}). Начались новые UTC-сутки или лимит подняли"
                        ),
                        environment=self.environment,
                        runbook_url=self.runbook_url,
                        emoji="✅",
                        tenant=usage.tenant_id,
                    )
                )
                continue
            if usage.tenant_id in self.llm_budget_alerted_tenants:
                continue
            self.llm_budget_alerted_tenants.add(usage.tenant_id)
            messages.append(
                format_alert(
                    error_code=ERR_LLM_BUDGET_NEAR_LIMIT,
                    title=f"дневной бюджет LLM израсходован на {share:.0%}",
                    detail=(
                        f"{spent} за текущие UTC-сутки "
                        f"(порог {self.llm_budget_ratio:.0%}). На 100% бот "
                        "перестаёт отвечать гостям: каждое сообщение уходит "
                        "в эскалацию к персоналу"
                    ),
                    environment=self.environment,
                    runbook_url=self.runbook_url,
                    tenant=usage.tenant_id,
                )
            )
        return messages

    def _evaluate_backup_freshness(self, probe: ProbeResult) -> list[str]:
        """Свежего бэкапа БД нет — ERR-OPS-008 (issue #106).

        Алерт состояния, как ERR-OPS-001/003/004: одно сообщение на вход в
        проблему и ✅ на выход. Отличие от линий по метрикам одно, и оно
        намеренное: «не знаю» здесь тоже алерт. У метрик молчание оправдано тем,
        что недоступное приложение уже покрыто ERR-OPS-001; нечитаемый каталог
        бэкапов не покрыт ничем — молчащий watchdog и есть режим отказа #106.
        """
        backup = probe.backup
        if backup is None:
            return []  # ALERT_BACKUP_DIR пуст — линия выключена (dev, CI)
        problem = _backup_problem(backup, max_age_seconds=self.backup_max_age_seconds)
        if problem is None:
            if not self.backup_alert_active:
                return []
            self.backup_alert_active = False
            # `problem is None` уже значит «каталог прочитан и дамп в нём есть»;
            # `or 0.0` стоит ради mypy, недостижимой ветки за ним нет.
            age = _humanize_seconds(backup.latest_age_seconds or 0.0)
            threshold = _humanize_seconds(self.backup_max_age_seconds)
            return [
                format_alert(
                    error_code=ERR_BACKUP_STALE,
                    title="свежий бэкап БД на месте",
                    detail=(
                        f"последний {_BACKUP_FILE_GLOB} в {backup.directory} снят "
                        f"{age} назад (порог {threshold})"
                    ),
                    environment=self.environment,
                    runbook_url=self.runbook_url,
                    emoji="✅",
                )
            ]
        if self.backup_alert_active:
            return []
        self.backup_alert_active = True
        title, detail = problem
        return [
            format_alert(
                error_code=ERR_BACKUP_STALE,
                title=title,
                detail=detail,
                environment=self.environment,
                runbook_url=self.runbook_url,
            )
        ]


def _backup_problem(backup: BackupProbe, *, max_age_seconds: float) -> tuple[str, str] | None:
    """(заголовок, детали) проблемы с бэкапом — или None, если бэкап свежий."""
    threshold = _humanize_seconds(max_age_seconds)
    if not backup.readable:
        return (
            "каталог бэкапов не виден алертеру",
            f"{backup.directory} не читается из контейнера ({backup.error}) — "
            "свежесть бэкапа не проверяется, и о его пропаже никто не узнает",
        )
    if backup.latest_age_seconds is None:
        return (
            "свежего бэкапа БД нет",
            f"в {backup.directory} нет ни одного {_BACKUP_FILE_GLOB} — ночной бэкап "
            "не создавался ни разу либо retention уже убрал всё старое",
        )
    if backup.latest_age_seconds > max_age_seconds:
        return (
            "свежего бэкапа БД нет",
            f"последний {_BACKUP_FILE_GLOB} в {backup.directory} снят "
            f"{_humanize_seconds(backup.latest_age_seconds)} назад (порог {threshold}). "
            "Ночной бэкап не идёт — восстанавливать в аварии будет нечего; снимки "
            "деплоя (pre-migrate-*) за бэкап не считаются",
        )
    return None


def probe_backups(directory: str, *, now: float) -> BackupProbe | None:
    """Свежайший ночной бэкап в каталоге; None — линия выключена (issue #106).

    ``now`` — стенные секунды (``time.time()``, не monotonic): сравнивается с
    временем изменения файлов. Ошибку чтения каталога возвращает значением, а не
    исключением: watchdog не имеет права падать из-за того, о чём должен
    доложить.
    """
    if not directory:
        return None
    try:
        entries = list(os.scandir(directory))
    except OSError as error:
        return BackupProbe(
            directory=directory,
            readable=False,
            latest_age_seconds=None,
            error=error.strerror or str(error),
        )
    newest_mtime: float | None = None
    for entry in entries:
        if not fnmatch(entry.name, _BACKUP_FILE_GLOB):
            continue
        # Файл мог унести retention между листингом и stat — это не отказ.
        with suppress(OSError):
            mtime = entry.stat().st_mtime
            newest_mtime = mtime if newest_mtime is None else max(newest_mtime, mtime)
    if newest_mtime is None:
        return BackupProbe(directory=directory, readable=True, latest_age_seconds=None)
    return BackupProbe(
        directory=directory,
        readable=True,
        # Отрицательный возраст (часы сервера ушли назад) — не «бэкап из
        # будущего», а ноль: свежее «только что» ничего не бывает.
        latest_age_seconds=max(0.0, now - newest_mtime),
    )


def probe_application(
    client: httpx.Client, base_url: str, *, backup: BackupProbe | None
) -> ProbeResult:
    """Один опрос приложения: /health/ready и /metrics.

    ``backup`` — уже снятый снимок каталога бэкапов (``probe_backups``): он
    приходит извне, потому что читается не по HTTP, а с диска. Аргумент
    обязателен намеренно, по правилу полей ``ProbeResult``: забытый параметр
    выключил бы линию молча, а так это ошибка mypy.
    """
    try:
        ready_response = client.get(f"{base_url}/health/ready")
        ready_ok = ready_response.status_code == 200
        ready_detail = ready_response.text.strip()
    except httpx.HTTPError as error:  # диагностический путь: сбой — это статус
        ready_ok = False
        ready_detail = f"connection error: {error}"

    metrics_text: str | None
    try:
        metrics_response = client.get(f"{base_url}/metrics")
        metrics_text = metrics_response.text if metrics_response.status_code == 200 else None
    except httpx.HTTPError:
        metrics_text = None

    if metrics_text is None:
        return ProbeResult(
            ready_ok=ready_ok,
            ready_detail=ready_detail,
            server_error_total=None,
            worker_heartbeat_age_seconds=None,
            outbox_pending_events=None,
            llm_budget_usage=[],
            backup=backup,
        )
    return ProbeResult(
        ready_ok=ready_ok,
        ready_detail=ready_detail,
        server_error_total=sum_server_errors(metrics_text),
        worker_heartbeat_age_seconds=read_gauge(metrics_text, "worker_heartbeat_age_seconds"),
        outbox_pending_events=read_gauge(metrics_text, "outbox_pending_events"),
        llm_budget_usage=read_llm_budget_usage(metrics_text),
        backup=backup,
    )


def send_telegram_message(client: httpx.Client, settings: Settings, text: str) -> None:
    """Отправить сообщение в Telegram-канал команды; сбой отправки логируется,
    но не роняет цикл (следующая итерация попробует снова при новом алерте)."""
    url, payload = alert_request(settings, text)
    try:
        response = client.post(url, json=payload)
        response.raise_for_status()
        logger.info("alert_sent", text=text)
    except httpx.HTTPError:
        logger.error("alert_send_failed", text=text, exc_info=True)


def run_alerter(
    iterations: int | None = None, *, transport: httpx.BaseTransport | None = None
) -> None:
    """Цикл алертера. ``iterations``/``transport`` — только для тестов."""
    settings = get_settings()
    # Fail-fast (§11) на полузаполненной паре — внутри alerting_configured.
    if not alerting_configured(settings):
        _run_disabled_loop(iterations)
        return

    monitor = AlertMonitor(
        ready_failure_threshold=settings.alert_ready_failure_threshold,
        error_spike_threshold=settings.alert_error_spike_threshold,
        cooldown_seconds=settings.alert_cooldown_seconds,
        environment=settings.sentry_environment,
        runbook_url=settings.alert_runbook_url,
        worker_heartbeat_max_age_seconds=settings.alert_worker_heartbeat_max_age_seconds,
        outbox_depth_threshold=settings.alert_outbox_depth_threshold,
        llm_budget_ratio=settings.alert_llm_budget_ratio,
        backup_max_age_seconds=settings.alert_backup_max_age_hours * 3600,
    )
    logger.info(
        "alerter_started",
        target=settings.alert_target_base_url,
        poll_interval_seconds=settings.alert_poll_interval_seconds,
        backup_dir=settings.alert_backup_dir or "выключено",
    )
    completed = 0
    with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS, transport=transport) as client:
        while iterations is None or completed < iterations:
            completed += 1
            probe = probe_application(
                client,
                settings.alert_target_base_url,
                backup=probe_backups(settings.alert_backup_dir, now=time.time()),
            )
            for message in monitor.evaluate(probe, now=time.monotonic()):
                send_telegram_message(client, settings, message)
            if iterations is None or completed < iterations:
                time.sleep(settings.alert_poll_interval_seconds)


def _run_disabled_loop(iterations: int | None) -> None:
    """Алертинг не сконфигурирован: процесс жив (деплой зелёный), напоминание
    в логах раз в час — неконфигурация видна, но не шумит."""
    completed = 0
    while iterations is None or completed < iterations:
        completed += 1
        logger.warning(
            "alerting_disabled",
            reason="TELEGRAM_ALERT_BOT_TOKEN/TELEGRAM_ALERT_CHAT_ID не заданы",
            runbook="docs/runbooks/alerts.md",
        )
        if iterations is None or completed < iterations:
            time.sleep(_DISABLED_REMINDER_SECONDS)


def main() -> None:  # pragma: no cover — точка входа процесса; логика покрыта run_alerter
    configure_logging(get_settings().log_level)
    run_alerter()


if __name__ == "__main__":  # pragma: no cover
    main()
