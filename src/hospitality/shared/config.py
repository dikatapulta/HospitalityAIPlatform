"""Типизированные настройки приложения (Task 0005, FOUNDATION P-7).

Единственный канонический способ прочитать конфигурацию окружения. Значения
читаются из переменных окружения / `.env` (см. `.env.example`); значения по
умолчанию совпадают с `.env.example`, чтобы `pytest`/`make check` работали без
дополнительной настройки, а `docker compose` (Task 0004) переопределял их
реальными значениями сети контейнеров.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import IPvAnyNetwork, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


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

    # Внешний адрес инсталляции (issue #65: постоянный вход через Cloudflare-туннель).
    # Уже был переменной деплоя — ей `ops/deploy/deploy.sh` регистрирует вебхук
    # Telegram; теперь её читает и приложение: из неё собирается АБСОЛЮТНАЯ ссылка
    # на политику конфиденциальности в тексте согласия (spec 0029 §2). Абсолютная,
    # потому что текст уходит в Telegram, где относительный путь бессмыслен.
    # Локальный дефолт совпадает с `alert_target_base_url` — тот же смысл «где
    # живёт это приложение».
    public_base_url: str = "http://localhost:8000"

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

    # Аренда строки outbox на время доставки (issue #134, ADR-016): сеть теперь
    # вне транзакции, и от второго диспетчера взятую строку держит эта отметка,
    # а не блокировка. Столько же ждёт возврата в очередь событие, чей воркер
    # умер посреди доставки, — поэтому не часы. Пачка, идущая дольше аренды,
    # даёт повторную доставку (P-8), а не потерю.
    worker_delivery_lease_seconds: float = 300.0

    # Retention терминальных строк outbox (issue #18, ADR-009; issue #133,
    # ADR-015; FOUNDATION §9): воркер периодически удаляет строки, доставленные
    # (processed_at) или похороненные (dead_lettered_at) больше
    # outbox_retention_days назад, проверяя раз в worker_cleanup_interval_seconds.
    outbox_retention_days: int = 30
    worker_cleanup_interval_seconds: float = 3600.0

    # Алерт о событиях, ушедших в dead-letter (issue #133, ADR-015): как часто
    # воркер докладывает в канал команды о похороненных событиях. Минута, а не
    # час очистки: непришедшее службе уведомление — сигнал минут, а не суток;
    # шум ограничивает не период, а пометка «о нём уже сказали».
    worker_dead_letter_alert_interval_seconds: float = 60.0

    # Пульс воркера (issue #136): как часто цикл обновляет отметку «я жив» в
    # таблице `worker_heartbeats`. Не «каждый круг»: при периоде опроса 1 с это
    # была бы запись в секунду ради сигнала, который читают раз в минуту.
    # 30 секунд — десятая часть порога устаревания ниже: пропуск одной-двух
    # записей (деплой, рестарт) в алерт не превращается.
    worker_heartbeat_interval_seconds: float = 30.0

    # Ретеншн гостевых текстов (issue #42, spec 0032, копия канона
    # OUTBOX_RETENTION_DAYS): воркер раз в worker_retention_interval_seconds
    # удаляет messages старше messages_retention_days, опустевшие давно не
    # обновлявшиеся conversations и обезличивает свободный текст заявок того же
    # возраста. 90 дней — обещание опубликованной политики конфиденциальности
    # (docs/legal/privacy-policy.md п. 7), поэтому срок один на инсталляцию,
    # а не конфиг тенанта.
    messages_retention_days: int = 90
    worker_retention_interval_seconds: float = 3600.0

    # Напоминания о невзятых заявках (issue #57, spec 0028): как часто воркер
    # ищет заявки, которые никто не взял дольше срока тенанта. Сам СРОК — конфиг
    # тенанта (`request_reminder_after_minutes`, P-11), здесь только частота
    # прогона: она — свойство инсталляции, как период очистки outbox. 5 минут
    # при типовом сроке 30 минут дают задержку сигнала не больше ~10%.
    worker_reminder_interval_seconds: float = 300.0

    # AI Gateway (Task 0014, FOUNDATION §7.2): единственная дверь к LLM.
    # Одна модель без маршрутизации (Non-Goal Task 0014); ключ провайдера —
    # только из окружения (docs/runbooks/secrets.md), пустой ключ валиден для
    # тестов/CI — боевой AnthropicProvider при нём не создастся.
    # `llm_model` — модель гостевого диалога (Task 0015). Sonnet 5 — выбор для
    # пилота (§7.7, ADR-010: приоритет казахского). Живой прогон при фиксе #71
    # показал: на казахском Haiku 4.5 лезет в русский (на вопрос про цену отвечал
    # по-русски) и коряво переводит, тогда как Sonnet 5 держит чистый kk и честно
    # отказывается выдумывать цену. Haiku втрое дешевле ($1/$5 vs $3/$15) — вернуть
    # его на простые ходы можно в Фазе 1 через маршрутизацию моделей (не Phase 0,
    # Non-Goal Task 0014). Выбор фиксирует bake-off (python -m ...ai.evals.bakeoff).
    anthropic_api_key: str = ""
    llm_model: str = "claude-sonnet-5"
    llm_timeout_seconds: float = 30.0
    llm_max_attempts: int = 3
    # Простейший бюджет Phase 0: один дневной лимит (USD, UTC-сутки) на КАЖДОГО
    # тенанта; превышение — отказ ERR-AI-002. Пер-тенантный бюджет — Phase 1.
    llm_tenant_daily_budget_usd: float = 5.0

    # Rate-limit гостевого чата (issue #41, spec 0023, §6): защита общего
    # LLM-бюджета тенанта (ERR-AI-002) от одного болтливого/злонамеренного чата.
    # Две ступени на chat_id: всплеск (N сообщений за окно) и дневной потолок
    # (UTC-сутки, окно не настраивается). Значение ≤0 отключает ступень.
    # Счётчики — Redis (shared/ratelimit.py, fail-open при его недоступности);
    # пер-тенантные значения — конфиг тенанта, Phase 1 (P-11).
    guest_chat_rate_limit_messages: int = 20
    guest_chat_rate_limit_window_seconds: int = 600
    guest_chat_rate_limit_messages_per_day: int = 200

    # Сессии кабинета персонала (spec 0033 §3.3, ADR-008 §1). Idle-срок
    # продлевается активностью (по last_used_at), absolute — жёсткий предел от
    # создания (expires_at). Долгие сроки осознанны: сессии живут на личных
    # телефонах персонала, частый re-login убьёт удобство (критерий приёмки
    # кабинета), а отзыв решается деактивацией пользователя, не TTL.
    staff_session_idle_ttl_days: int = 30
    staff_session_absolute_ttl_days: int = 180

    # Срок жизни ссылки-приглашения сотрудника (spec 0033 §3.4): ссылку
    # менеджер передаёт сам (WhatsApp/лично), 72 часов хватает «переслал —
    # открыл вечером»; истёкший инвайт перевыпускается новой ссылкой.
    staff_invite_ttl_hours: int = 72

    # Rate-limit логина кабинета (spec 0033 §3.3, канон Redis-счётчика 0023):
    # два ключа на попытку — нормализованный email (подбор пароля к одной
    # учётке) и IP (перебор учёток с одного клиента). Тратят бюджет только
    # НЕУДАЧНЫЕ попытки (issue #207). Бюджеты разные, потому что разный
    # субъект: за email стоит один человек, за IP — весь отель, ушедший в
    # интернет через один адрес (NAT), поэтому IP-бюджет просторный. Значение
    # ≤0 отключает свой ключ (страховочный люк, как у гостевых лимитов).
    staff_login_rate_limit_attempts: int = 10
    staff_login_ip_rate_limit_attempts: int = 60
    staff_login_rate_limit_window_seconds: int = 600

    # Лимит попыток ввода кода заселения в веб-чате (spec 0027 §3.3, ADR-008):
    # ключ — (tenant, room), а не клиент — перебор с многих клиентов не обходит
    # лимит. Значение ≤0 отключает (страховочный люк, как ступени выше).
    guest_code_verify_rate_limit_attempts: int = 10
    guest_code_verify_rate_limit_window_seconds: int = 600

    # Rate-limit'ы одноразовой ссылки привязки (spec 0033 §6/§9, канон 0023).
    # Выпуск — по (tenant, stay): кнопка на карточке заселения; лимит ловит
    # залипший скрипт, а не человека. Потребление — по IP: у анонима с QR нет
    # ключа тенанта; лимит просторный — гости за NAT отеля делят один адрес.
    # Значение ≤0 отключает (страховочный люк, как у лимитов выше).
    guest_bind_link_issue_rate_limit_attempts: int = 30
    guest_bind_link_issue_rate_limit_window_seconds: int = 600
    guest_bind_link_consume_rate_limit_attempts: int = 60
    guest_bind_link_consume_rate_limit_window_seconds: int = 600

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
    # персонал закрывает их командами `/start|/done|/cancel <#N>` (ADR-013). Входящий
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
    # Две линии на один симптом «доставка событий встала» (issue #136).
    # Первая — возраст пульса воркера (`worker_heartbeat_age_seconds` в
    # /metrics): старше порога → ERR-OPS-003. 5 минут — десять пропущенных
    # пульсов: рестарт воркера на деплое (секунды) ложным алертом не станет,
    # а мёртвый процесс виден заметно раньше, чем гость успеет пожаловаться.
    # Вторая — глубина очереди (`outbox_pending_events`): выше порога →
    # ERR-OPS-004. Она ловит и то, чего пульс не видит: живой цикл, который
    # не справляется или ходит по кругу неудачных доставок. 100 событий —
    # заведомо выше рабочего фона отеля (очередь разбирается за секунды).
    alert_worker_heartbeat_max_age_seconds: float = 300.0
    alert_outbox_depth_threshold: int = 100
    # Ссылка на runbook в каждом алерте (§10.8: алерт обязан вести к диагнозу).
    alert_runbook_url: str = (
        "https://github.com/dikatapulta/HospitalityAIPlatform/blob/main/docs/runbooks/alerts.md"
    )

    # Доверенные прокси (issue #207): список IP/CIDR через запятую, чьему
    # заголовку `CF-Connecting-IP` приложение верит как адресу клиента
    # (`shared/clientip.py`). Пусто (дефолт локальной разработки) — не верить
    # никому, адрес берётся из сокета. На staging сюда идёт подсеть
    # docker-сети: единственный сосед app по ней — cloudflared, наружу порт
    # не открыт. Типизация сетью, а не строкой: опечатка в CIDR обязана
    # падать на старте — иначе она молча вернула бы дефект #207 (весь отель
    # в одном ключе rate-limit), и заметить это было бы нечем.
    # `NoDecode` — список в env через запятую (как привыкли ops), а не
    # JSON-массивом, который pydantic-settings иначе ждёт от list.
    trusted_proxy_ips: Annotated[list[IPvAnyNetwork], NoDecode] = []

    @field_validator("trusted_proxy_ips", mode="before")
    @classmethod
    def _split_trusted_proxy_ips(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

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
