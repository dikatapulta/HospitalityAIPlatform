"""Настройка из `.env.example` доходит до контейнера (issue #261, #263; docs-drift H-2).

Окружение сервисов в обоих compose-файлах перечислено поимённо: без строки
`NAME: ${NAME:-…}` значение из `.env` до контейнера не доходит, а код молча
работает на умолчании `shared/config.py`. Рунбук велит «поднять в `.env`» — и
в разгар инцидента ничего не меняется.

Каждое имя из `.env.example` (и закомментированное `# NAME=` — это тоже ручка)
записано здесь ровно одним решением: какие процессы его читают — тогда строка
обязана стоять в env каждого из них в обоих compose, — или почему оно не
пробрасывается вовсе. Новое имя без решения — красный тест: «эту не
пробрасываем» записывается, а не подразумевается. Равенство умолчаний compose
и кода для лимитов попыток сверяет `test_compose_rate_limits.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ENV_EXAMPLE = _ROOT / ".env.example"
_NAME_LINE = re.compile(r"^(?:# )?([A-Z][A-Z0-9_]*)=", re.MULTILINE)

_APP = frozenset({"app"})
_WORKER = frozenset({"worker"})
_ALERTER = frozenset({"alerter"})
_APP_WORKER = _APP | _WORKER
_ALL = _APP | _WORKER | _ALERTER

# Кто читает имя. Лишняя строка в env сервиса тест не роняет (у staging-воркера
# есть, например, модель «Наставника» — для единообразия с app); недостающая — да.
_READERS: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "POSTGRES_PORT", "REDIS_PORT"),
        _APP_WORKER,
    ),
    **dict.fromkeys(("PUBLIC_BASE_URL", "SERVICE_TOKEN", "SERVICE_TOKEN_TENANT_SLUG"), _APP),
    # Шлюз модели: гость — у app, перевод сути заявки и фото реквизитов — у воркера.
    **dict.fromkeys(
        (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "LLM_MODEL",
            "LLM_TIMEOUT_SECONDS",
            "LLM_MAX_ATTEMPTS",
            "LLM_RETRY_BACKOFF_BASE_SECONDS",
            "LLM_TENANT_DAILY_BUDGET_USD",
            "LLM_MODEL_DOCUMENTS",
        ),
        _APP_WORKER,
    ),
    **dict.fromkeys(
        ("LLM_MODEL_MENTOR", "LLM_MENTOR_DAILY_BUDGET_USD", "LLM_EMBEDDING_MODEL"), _APP
    ),
    # Помощник 3D-витрины (spec 0069 §2.3) зовёт модель только из app.
    **dict.fromkeys(("LLM_MODEL_SHOWROOM", "LLM_SHOWROOM_DAILY_BUDGET_USD"), _APP),
    # «Предложить идею» (spec 0071 §7) — тоже только из app (кабинет).
    **dict.fromkeys(("LLM_MODEL_IDEAS", "LLM_IDEAS_DAILY_BUDGET_USD"), _APP),
    "LLM_DOCUMENTS_DAILY_BUDGET_USD": _WORKER,
    **dict.fromkeys(
        (
            "ENABLE_SERVICE_REQUESTS",
            "ENABLE_DEMO_HANDOVER",
            "ENABLE_ROOM_BOARD",
            "ENABLE_HANDOVER_MANUAL",
            "ENABLE_DEMO_LOST_FOUND",
            "DEMO_MENTOR_URL",
            "DEMO_CODEX_URL",
            "DEMO_3D_URL",
            "ENABLE_STAFF_STATS",
            "ENABLE_STAFF_CONVERSATIONS",
            "ENABLE_GUEST_DOCUMENTS",
        ),
        _APP,
    ),
    **dict.fromkeys(
        (
            "ENABLE_TURNOVER_BOARD",
            "ENABLE_HOUSEKEEPING_FLOORS",
            "FILE_STORAGE_BACKEND",
            "FILE_STORAGE_ROOT",
            "FILE_ENCRYPTION_KEY",
            "STAFF_PUSH_BACKEND",
            "STAFF_PUSH_VAPID_PRIVATE_KEY",
            "PMS_BACKEND",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_STAFF_CHAT_ID",
        ),
        _APP_WORKER,
    ),
    # Лимиты и сроки входа гостя и персонала — рычаги рунбуков errors.md.
    **dict.fromkeys(
        (
            "GUEST_CHAT_RATE_LIMIT_MESSAGES",
            "GUEST_CHAT_RATE_LIMIT_WINDOW_SECONDS",
            "GUEST_CHAT_RATE_LIMIT_MESSAGES_PER_DAY",
            "GUEST_CHAT_DAILY_BUDGET_SHARE",
            "GUEST_CODE_VERIFY_RATE_LIMIT_ATTEMPTS",
            "GUEST_CODE_VERIFY_RATE_LIMIT_WINDOW_SECONDS",
            "GUEST_CODE_VERIFY_IP_RATE_LIMIT_ATTEMPTS",
            "GUEST_BIND_LINK_ISSUE_RATE_LIMIT_ATTEMPTS",
            "GUEST_BIND_LINK_ISSUE_RATE_LIMIT_WINDOW_SECONDS",
            "GUEST_BIND_LINK_CONSUME_RATE_LIMIT_ATTEMPTS",
            "GUEST_BIND_LINK_CONSUME_RATE_LIMIT_WINDOW_SECONDS",
            "STAFF_SESSION_IDLE_TTL_DAYS",
            "STAFF_SESSION_ABSOLUTE_TTL_DAYS",
            "STAFF_SESSION_SHORT_IDLE_MINUTES",
            "STAFF_INVITE_TTL_HOURS",
            "STAFF_LOGIN_RATE_LIMIT_ATTEMPTS",
            "STAFF_LOGIN_IP_RATE_LIMIT_ATTEMPTS",
            "STAFF_LOGIN_IP_FLOOD_RATE_LIMIT_ATTEMPTS",
            "STAFF_LOGIN_RATE_LIMIT_WINDOW_SECONDS",
            "STAFF_SUPERADMIN_SESSION_TTL_MINUTES",
            "STAFF_PASSWORD_RESET_TTL_HOURS",
            "TRUSTED_PROXY_IPS",
            "TELEGRAM_WEBHOOK_SECRET",
            "TELEGRAM_TENANT_SLUG",
        ),
        _APP,
    ),
    **dict.fromkeys(("SENTRY_DSN", "SENTRY_ENVIRONMENT", "LOG_LEVEL"), _ALL),
    # Алерт о dead-letter и копию утренней сводки шлёт воркер, остальное — алертер.
    **dict.fromkeys(("TELEGRAM_ALERT_BOT_TOKEN", "TELEGRAM_ALERT_CHAT_ID"), _WORKER | _ALERTER),
    **dict.fromkeys(
        (
            "ALERT_POLL_INTERVAL_SECONDS",
            "ALERT_READY_FAILURE_THRESHOLD",
            "ALERT_ERROR_SPIKE_THRESHOLD",
            "ALERT_COOLDOWN_SECONDS",
            "ALERT_LOGIN_RATE_LIMITED_THRESHOLD",
            "ALERT_WORKER_HEARTBEAT_MAX_AGE_SECONDS",
            "ALERT_OUTBOX_DEPTH_THRESHOLD",
            "ALERT_LLM_BUDGET_RATIO",
            "ALERT_LLM_FAILURE_STREAK_THRESHOLD",
            "ALERT_GUEST_DOOR_THRESHOLD",
            "ALERT_BACKUP_DIR",
            "ALERT_BACKUP_MAX_AGE_HOURS",
            "ALERT_DISK_PATH",
            "ALERT_DISK_MIN_FREE_RATIO",
            "ALERT_GUEST_TEXT_RETENTION_GRACE_HOURS",
            "ALERT_HEARTBEAT_URL",
        ),
        _ALERTER,
    ),
    **dict.fromkeys(
        (
            "WORKER_POLL_INTERVAL_SECONDS",
            "WORKER_BATCH_SIZE",
            "WORKER_MAX_DELIVERY_ATTEMPTS",
            "WORKER_RETRY_BACKOFF_BASE_SECONDS",
            "WORKER_RETRY_BACKOFF_MAX_SECONDS",
            "WORKER_DELIVERY_LEASE_SECONDS",
            "OUTBOX_RETENTION_DAYS",
            "WORKER_CLEANUP_INTERVAL_SECONDS",
            "WORKER_DEAD_LETTER_ALERT_INTERVAL_SECONDS",
            "WORKER_HEARTBEAT_INTERVAL_SECONDS",
            "WORKER_RETENTION_INTERVAL_SECONDS",
            "WORKER_REMINDER_INTERVAL_SECONDS",
            "WORKER_DAILY_SUMMARY_INTERVAL_SECONDS",
            "WORKER_STAY_AUTO_CHECKOUT_INTERVAL_SECONDS",
            "WORKER_PMS_SYNC_INTERVAL_SECONDS",
            "WORKER_STAFF_PUSH_INTERVAL_SECONDS",
        ),
        _WORKER,
    ),
}

# Не пробрасывается ни одному сервису — и почему.
_NOT_PASSED: dict[str, str] = {
    "APP_PORT": "порт приложения на хосте: его читает `ports:` dev-compose, а не процесс",
    "STAGING_HOST": "доступ к staging с машины основателя (make smoke-staging, backup-fetch)",
    "STAGING_SSH_KEY": "доступ к staging с машины основателя (make smoke-staging, backup-fetch)",
    "STAGING_BASE_URL": "адрес staging для make smoke-staging с машины основателя",
    "BACKUP_AGE_KEY": "приватный ключ бэкапов живёт только у основателя (spec 0024)",
    "MESSAGES_RETENTION_DAYS": (
        "срок — обещание опубликованной политики (spec 0032): один на инсталляцию и "
        "меняется кодом вместе с её текстом; воркер (ретеншн), app и алертер "
        "(ERR-OPS-017) читают одно умолчание shared/config.py, строка .env у одного "
        "из них разошлась бы с остальными"
    ),
}

# Значение задаёт сам compose, а не `.env`, — сознательно.
_FIXED_BY_COMPOSE: dict[str, str] = {
    "POSTGRES_PORT": "в сети compose — порт контейнера db; в .env — порт на хосте",
    "REDIS_PORT": "в сети compose — порт контейнера redis; в .env — порт на хосте",
    "FILE_STORAGE_ROOT": "путь тома files_data внутри контейнера",
    "STAFF_PUSH_BACKEND": "staging — настоящие службы пуша (vapid)",
    "SENTRY_ENVIRONMENT": "имя контура staging — префикс алертов и окружение Sentry",
    "ALERT_BACKUP_DIR": "каталог бэкапов хоста, смонтированный в алертер; пусто выключило бы линию",
    "ALERT_DISK_PATH": "тот же смонтированный каталог; пусто выключило бы линию",
}

# Сервис, который в этом compose сознательно не получает имя, — и почему.
# Локальный воркер без ключа и модели: `smoke.yml` поднимает этот compose с
# настоящим ключом, и вызовы модели воркером (перевод сути заявки, фото
# реквизитов) стали бы платными вызовами каждого прогона smoke сверх его цены
# (spec 0019). Перевод без ключа штатно отдаёт оригинал; на staging ключ у воркера есть.
# Модель фото тогда нет и у локального app: по ней форма говорит гостю «фото
# прочитает ИИ», а читает воркер — строка одна у обоих или ни у кого
# (`channels/web/tests/test_documents.py`).
_DEV_WORKER_WITHOUT_MODEL = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "LLM_MODEL",
        "LLM_MODEL_DOCUMENTS",
        "LLM_DOCUMENTS_DAILY_BUDGET_USD",
    }
)

_COMPOSES = {
    "ops/docker-compose.yml": (
        _APP_WORKER,
        {"worker": _DEV_WORKER_WITHOUT_MODEL, "app": frozenset({"LLM_MODEL_DOCUMENTS"})},
    ),
    "ops/deploy/docker-compose.staging.yml": (_ALL, {}),
}


def _env_names() -> set[str]:
    return set(_NAME_LINE.findall(_ENV_EXAMPLE.read_text(encoding="utf-8")))


def _service_env(path: Path) -> dict[str, dict[str, str]]:
    """`environment:` каждого сервиса; разметка — как в обоих compose (отступ 2/4/6).

    Формат списка (`- NAME=value`) или иной отступ сюда не попадут — и тест
    покраснеет на отсутствующих именах, а не пропустит их молча.
    """
    services: dict[str, dict[str, str]] = {}
    service: str | None = None
    in_services = in_env = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_services, service, in_env = stripped == "services:", None, False
        elif in_services and indent == 2:
            service, in_env = stripped.rstrip(":"), False
            services[service] = {}
        elif in_services and indent == 4:
            in_env = stripped == "environment:"
        elif in_env and indent == 6 and service is not None:
            name, _, value = stripped.partition(":")
            services[service][name] = value.strip()
    return services


def test_every_env_example_name_has_one_decision() -> None:
    names = _env_names()
    decided = _READERS.keys() | _NOT_PASSED.keys()

    undecided, stale = sorted(names - decided), sorted(decided - names)
    assert not undecided, f"имена без решения — в _READERS или _NOT_PASSED: {undecided}"
    assert not stale, f"решения о несуществующих именах: {stale}"
    assert not _READERS.keys() & _NOT_PASSED.keys(), sorted(_READERS.keys() & _NOT_PASSED.keys())


@pytest.mark.parametrize("compose", sorted(_COMPOSES))
def test_env_example_names_reach_their_readers(compose: str) -> None:
    expected, gaps = _COMPOSES[compose]
    services = _service_env(_ROOT / compose)
    assert expected <= services.keys(), f"нет сервисов {sorted(expected - services.keys())}"

    problems: list[str] = []
    for name in sorted(_env_names() & _READERS.keys()):
        for service in sorted(_READERS[name] & expected):
            env = services[service]
            if name in gaps.get(service, frozenset()):
                if name in env:
                    problems.append(f"{service}.{name}: записан пропуском, а строка есть")
                continue
            value = env.get(name)
            if value is None:
                problems.append(f"{service}.{name}: нет строки — значение .env не дойдёт")
            elif name not in _FIXED_BY_COMPOSE and not re.match(rf"\$\{{{name}[:}}]", value):
                problems.append(f"{service}.{name}: значение {value!r}, а не ${{{name}…}}")
    for name in sorted(_NOT_PASSED):
        passed_to = [s for s in sorted(services) if name in services[s]]
        problems.extend(f"{s}.{name}: записан непробрасываемым" for s in passed_to)

    assert not problems, "\n".join(problems)
