"""Типизированные настройки приложения (Task 0005, FOUNDATION P-7).

Единственный канонический способ прочитать конфигурацию окружения. Значения
читаются из переменных окружения / `.env` (см. `.env.example`); значения по
умолчанию совпадают с `.env.example`, чтобы `pytest`/`make check` работали без
дополнительной настройки, а `docker compose` (Task 0004) переопределял их
реальными значениями сети контейнеров.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "hospitality"
    postgres_password: str = "hospitality"
    postgres_db: str = "hospitality"

    redis_host: str = "localhost"
    redis_port: int = 6379

    app_port: int = 8000

    # Аутентификация HTTP API (Task 0013, FOUNDATION §11): статический сервисный
    # токен Phase 0 — один системный клиент, привязанный к одному тенанту по slug
    # (клиент не выбирает себе тенанта, §11). Значение по умолчанию — только для
    # локальной разработки и тестов; на staging токен обязан быть заменён
    # случайным (ops/deploy/.env.staging.example, docs/runbooks/secrets.md).
    service_token: str = "dev-service-token"
    service_token_tenant_slug: str = "demo-hotel"

    # Воркер доменных событий (Task 0010, ADR-005): период опроса outbox при
    # пустой очереди, размер пачки и предел попыток доставки одного события
    # (исчерпание — ERR-EVENTS-002 в docs/runbooks/errors.md).
    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 50
    worker_max_delivery_attempts: int = 10

    # Backoff между попытками доставки одного события (issue #18, ADR-009):
    # после неудачи следующая попытка не раньше, чем через
    # min(base * 2**(attempts-1), max) секунд.
    worker_retry_backoff_base_seconds: float = 2.0
    worker_retry_backoff_max_seconds: float = 300.0

    # Retention обработанных строк outbox (issue #18, ADR-009, FOUNDATION §9):
    # воркер периодически удаляет строки с processed_at старше
    # outbox_retention_days, проверяя раз в worker_cleanup_interval_seconds.
    outbox_retention_days: int = 30
    worker_cleanup_interval_seconds: float = 3600.0

    # AI Gateway (Task 0014, FOUNDATION §7.2): единственная дверь к LLM.
    # Одна модель без маршрутизации (Non-Goal Task 0014); ключ провайдера —
    # только из окружения (docs/runbooks/secrets.md), пустой ключ валиден для
    # тестов/CI — боевой AnthropicProvider при нём не создастся.
    # `llm_model` — модель гостевого диалога (Task 0015). Haiku 4.5 — выбор
    # bake-off'а на 6 языках (spec 0015, §7.7, ADR-010): и Haiku, и Sonnet 5
    # прошли P1-бар (не выдумывают цены/правила ни на одном из 6 языков, вкл. kk)
    # и дают корректный казахский. При равном качестве Haiku втрое дешевле
    # ($1/$5 vs $3/$15). Пересмотр — при росте golden-set / смене демографии.
    anthropic_api_key: str = ""
    llm_model: str = "claude-haiku-4-5"
    llm_timeout_seconds: float = 30.0
    llm_max_attempts: int = 3
    # Простейший бюджет Phase 0: один дневной лимит (USD, UTC-сутки) на КАЖДОГО
    # тенанта; превышение — отказ ERR-AI-002. Пер-тенантный бюджет — Phase 1.
    llm_tenant_daily_budget_usd: float = 5.0

    # Канал Telegram (Task 0016, §8.4). `telegram_webhook_secret` — секрет вебхука:
    # Telegram шлёт его в заголовке `X-Telegram-Bot-Api-Secret-Token` на каждом
    # запросе (задаётся при setWebhook); пустой = вебхук закрыт и отвергает всё
    # (fail-closed, §11). `telegram_bot_token` — токен бота для отправки ответов
    # (пустой валиден для тестов: они подставляют фейк-отправитель). `telegram_
    # tenant_slug` — маппинг чата на тенанта Phase 0 (один бот = демо-тенант, как
    # `service_token_tenant_slug`). `telegram_api_base_url` — база Bot API
    # (в тестах отправитель фейковый; переопределяется только для локального стенда).
    telegram_webhook_secret: str = ""
    telegram_bot_token: str = ""
    telegram_tenant_slug: str = "demo-hotel"
    telegram_api_base_url: str = "https://api.telegram.org"
    # Staff-чат службы (Task 0017, сквозная сборка). `chat.id` Telegram-чата, куда
    # подписчик события `request.created` шлёт уведомления о новых заявках и где
    # персонал закрывает их командами `/assign|/start|/done|/cancel <id>`. Входящий
    # чат с этим id канал трактует как команды персонала, а не как реплики гостя
    # оркестратору. Пусто = уведомления службе выключены (для staging скелета чат
    # обязателен). Строка, а не int: chat.id групп бывает отрицательным, а сравнение
    # с chat_id гостя — строковое (как `Conversation.external_id`). Кабинет
    # персонала и RBAC — Phase 1 (§17.7, ADR-011).
    telegram_staff_chat_id: str = ""

    # Наблюдаемость (Task 0018, FOUNDATION §10.4, §10.12). Пустой SENTRY_DSN —
    # Sentry выключен (dev/CI работают без внешнего сервиса); DSN — не секрет
    # в строгом смысле, но живёт в .env как весь конфиг окружения.
    # SENTRY_ENVIRONMENT разделяет события dev/staging/prod в одном проекте.
    sentry_dsn: str = ""
    sentry_environment: str = "dev"

    # Алертер (Task 0018, §10.8): watchdog-процесс `hospitality.tools.alerter`
    # опрашивает /health/ready и /metrics приложения и шлёт алерты в
    # Telegram-канал команды. Токен может совпадать с TELEGRAM_BOT_TOKEN
    # (тот же бот, другой чат). Оба пустые = алертер пассивен (WARNING в лог);
    # заполнен только один — ошибка конфигурации, немедленное падение.
    telegram_alert_bot_token: str = ""
    telegram_alert_chat_id: str = ""
    alert_target_base_url: str = "http://localhost:8000"
    alert_poll_interval_seconds: float = 60.0
    # Сколько опросов /health/ready подряд должны провалиться до алерта
    # ERR-OPS-001 (защита от одиночного сетевого чиха) и какой прирост 5xx за
    # один интервал считается всплеском ERR-OPS-002; cooldown ограничивает
    # частоту повторных алертов о всплесках.
    alert_ready_failure_threshold: int = 2
    alert_error_spike_threshold: int = 5
    alert_cooldown_seconds: float = 900.0
    # Ссылка на runbook в каждом алерте (§10.8: алерт обязан вести к диагнозу).
    alert_runbook_url: str = (
        "https://github.com/dikatapulta/HospitalityAIPlatform/blob/main/docs/runbooks/alerts.md"
    )

    # Literal, а не str: опечатка в LOG_LEVEL должна падать здесь внятной ошибкой
    # конфигурации, а не ValueError из глубин logging при старте (crash-loop
    # контейнера с непонятным трейсбеком).
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        # LOG_LEVEL=info или значение с пробелом — валидная конфигурация.
        return value.strip().upper() if isinstance(value, str) else value

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_dsn_async(self) -> str:
        # DSN для SQLAlchemy async engine (Task 0008): тот же Postgres,
        # но с явным драйвером asyncpg в схеме URL.
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_dsn(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
