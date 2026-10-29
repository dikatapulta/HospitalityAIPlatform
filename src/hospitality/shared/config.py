"""Типизированные настройки приложения (Task 0005, FOUNDATION P-7).

Единственный канонический способ прочитать конфигурацию окружения. Значения
читаются из переменных окружения / `.env` (см. `.env.example`); значения по
умолчанию совпадают с `.env.example`, чтобы `pytest`/`make check` работали без
дополнительной настройки, а `docker compose` (Task 0004) переопределял их
реальными значениями сети контейнеров.
"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, IPvAnyNetwork, field_validator
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

    # Утреннее сообщение со сводкой дня (issue #301, spec 0035 §8): как часто
    # воркер проверяет, не наступило ли у кого-то из отелей локальное время
    # рассылки. САМО ВРЕМЯ — конфиг тенанта (`daily_summary_local_time`, P-11),
    # здесь только частота проверки. 10 минут — точность попадания в 09:00,
    # которой для утренней сводки достаточно; планировщика не заводим (NG-8).
    worker_daily_summary_interval_seconds: float = 600.0

    # Автовыезд по сроку (issue #225, spec 0043): как часто воркер закрывает
    # Stay, у которых вышел запас после расчётного часа. Сам ЗАПАС — конфиг
    # тенанта (`stay_auto_checkout_grace_hours`, P-11), здесь только частота.
    # 10 минут против запаса в часы — комната освобождается практически вовремя.
    worker_stay_auto_checkout_interval_seconds: float = 600.0

    # AI Gateway (Task 0014, FOUNDATION §7.2): единственная дверь к LLM.
    # Одна модель без маршрутизации (Non-Goal Task 0014); ключ провайдера —
    # только из окружения (docs/runbooks/secrets.md), пустой ключ валиден для
    # тестов/CI — боевой AnthropicProvider при нём не создастся.
    # `llm_model` — модель гостевого диалога (Task 0015). Sonnet 5 — выбор для
    # пилота (§7.7, ADR-010: приоритет казахского). Живой прогон при фиксе #71
    # показал: на казахском Haiku 4.5 лезет в русский (на вопрос про цену отвечал
    # по-русски) и коряво переводит, тогда как Sonnet 5 держит чистый kk и честно
    # отказывается выдумывать цену. Haiku дешевле по тарифу (прайс-лист шлюза), но
    # на нашем промпте вызов выходит дешевле лишь на ~10%: токенов на тот же текст
    # у него почти вдвое больше (журнал 10.09.2026, issue #348). Вернуть его на
    # простые ходы можно в Фазе 1 через маршрутизацию моделей (не Phase 0,
    # Non-Goal Task 0014). Выбор фиксирует bake-off (python -m ...ai.evals.bakeoff).
    anthropic_api_key: str = ""
    # Ключ второго провайдера (ADR-020): провайдер выводится из `llm_model` по
    # прайс-листу шлюза. Пустой валиден, пока модель — не OpenAI; модель OpenAI
    # без ключа процесс не поднимет (fail-fast старта, `validate_configured_model`).
    openai_api_key: str = ""
    llm_model: str = "claude-sonnet-5"
    llm_timeout_seconds: float = 30.0
    # `ge=1` — не украшение (issue #273): при нуле цикл попыток в `complete()`
    # не выполняется ни разу, и вызов падает на `assert result is not None`, то
    # есть гость получает 500 вместо кода каталога (R-8). Граница стоит на
    # границе конфигурации, а не защитой в коде: процесс не поднимается с
    # бессмысленной настройкой — как и с моделью вне прайс-листа (issue #137).
    # Канон значения с границей — `LlmRequest.max_tokens` (`Field(gt=0)`).
    llm_max_attempts: int = Field(default=3, ge=1)
    # Пауза перед следующей попыткой после таймаута (issue #46, ADR-017): база
    # экспоненты, как у доставки outbox (ADR-009, `worker_retry_backoff_base_seconds`).
    # Потолок паузы отдельной настройкой не заводится — им служит
    # `llm_timeout_seconds`: ждать дольше одной попытки на ходе гостя, который
    # ждёт ответа прямо сейчас, бессмысленно.
    llm_retry_backoff_base_seconds: float = 1.0
    # Простейший бюджет Phase 0: один дневной лимит (USD, UTC-сутки) на КАЖДОГО
    # тенанта; превышение — отказ ERR-AI-002. Пер-тенантный бюджет — Phase 1.
    # $10 вместо прежних $5 (issue #125): у пилотного отеля ~50–100 диалогов в
    # сутки по 4–8 ходов на Sonnet 5 — это $1,2–4,8/день, то есть верх оценки
    # упирался в прежний потолок и отель замолкал в свой самый нужный день.
    # $10 — двойной запас к верху оценки и при этом потолок, а не план траты:
    # платится фактический расход, а приближение к потолку теперь видно заранее
    # (ERR-OPS-006 на ALERT_LLM_BUDGET_RATIO). Число подтверждает основатель —
    # docs/FOUNDER_REVIEW_QUEUE.md.
    llm_tenant_daily_budget_usd: float = 10.0
    # Назначение `mentor` — «Наставник» персонала (ADR-024, spec 0048): своя
    # модель и свой дневной бюджет тенанта, отдельно от гостевого. Пустая модель —
    # назначение выключено (ERR-AI-006): dev и CI поднимаются без ключа OpenAI.
    # Заданная модель обязана быть в прайс-листе чатов, уметь поток и иметь ключ
    # провайдера — иначе процесс не стартует (`validate_configured_model`).
    # $2 в день — потолок решения 27.10 (spec 0048 §8: ~$0,4 в день при 100
    # сотрудниках по 3 вопроса), а не план траты.
    llm_model_mentor: str = ""
    llm_mentor_daily_budget_usd: float = 2.0
    # Назначение `documents` — черновик реквизитов для АВР по фото гостя (spec 0049
    # Р-13): своя модель и свой дневной бюджет тенанта. Пустая модель — выключено,
    # поля фото заполняет бухгалтер; так dev, CI и пилот до правки политики (§8).
    # Заданная модель обязана быть в прайс-листе и у провайдера с картинками
    # (`IMAGE_PROVIDERS`) — иначе процесс не стартует. $1 в день — потолок: фото
    # стоит около цента, а 25 фото одного проживания — меньше $0,3.
    llm_model_documents: str = ""
    llm_documents_daily_budget_usd: float = 1.0
    # Модель эмбеддингов (поиск статьи кодекса по смыслу, ADR-024 §3). Ключ
    # группы эмбеддингов прайс-листа; размерность 1536 зашита в схему
    # (`vector(1536)`), смена модели — миграция плюс пересчёт статей.
    llm_embedding_model: str = "text-embedding-3-small"

    # Режим «только консультации»: включён ли AI-инструмент создания заявки
    # (`ai/tools/create_service_request.py`). False — инструмент НЕ попадает в
    # запрос к модели вовсе (`tools.registry.build_tool_specs`), и вместо
    # раздела «Service requests» промпта модель получает блок-запрет
    # (`orchestrator._consultation_only_block`): модель не знает об инструменте
    # и не обещает гостю действий, которых не совершит. Умолчание — выключено:
    # обратно включается одной строкой `ENABLE_SERVICE_REQUESTS=true` в
    # окружении, код инструмента и канал Telegram при этом не трогаются.
    enable_service_requests: bool = False

    # Демо «Передача смены» к собранию в отеле 19.10 (issue #450): касса к сдаче
    # смены одним кликом в кабинете на выдуманных данных — без БД, без модели и
    # без интеграций. False — страницы нет (404), карточки на главной нет.
    # Включается только на staging одной строкой `ENABLE_DEMO_HANDOVER=true`.
    enable_demo_handover: bool = False

    # Касса передачи смены — только ручной бланк (issue #469): та же страница
    # без примера и без листа передачи, суммы вписывает сотрудник и печатает;
    # без БД, модели и интеграций. Для гостевой копии. Включённое демо
    # (выше) главнее: на staging остаётся демо целиком.
    enable_handover_manual: bool = False

    # Доска «Выезды и уборка» (spec 0046 §5, issue #476): False — раздела нет
    # (404, в меню пункта нет), «Выселить» только выселяет. Служба «Уборка» у
    # сотрудников доску сама не включает: на гостевой копии — только после
    # согласования с отелем (docs/runbooks/pilot-copy.md).
    enable_turnover_board: bool = False

    # Приём реквизитов для АВР от гостя (spec 0049 Р-7): где лежат документы
    # гостя — свойство контура и его хостинга, не отеля. False — скрепки и
    # маршрутов формы нет (PR 4 серии), пункта «Документы для АВР» в меню нет;
    # сама страница по ссылке открывает прошлые запросы до их срока. Отель
    # включает функцию данными — службой «Бухгалтерия» (`accounting`).
    enable_guest_documents: bool = False

    # Хранилище файлов (spec 0047, ADR-022): фото блюд и товаров, файлы гостя,
    # фото к заявке. `fake` — память процесса (ADR-007: dev, CI, тесты);
    # `local` — каталог `file_storage_root`, в compose — именованный том
    # `files_data`. Compose-файлы задают `local` явно: Fake при двух воркерах
    # uvicorn делил бы файлы между процессами и терял их на рестарте.
    file_storage_backend: Literal["local", "fake"] = "fake"
    file_storage_root: str = "/var/lib/hospitality/files"
    # Ключ AES-256-GCM файлов на томе (FOUNDATION §11: документы и реквизиты
    # шифруются приложением): 32 байта в base64 — `openssl rand -base64 32`.
    # Пусто — приём и выдача файлов отвечают ERR-FILES-005, остальное работает.
    # Секрет: docs/runbooks/secrets.md; потеря ключа = потеря файлов.
    # Формат проверяет preflight, а не валидатор поля: ValidationError pydantic
    # печатает `input_value`, то есть секрет, в лог контейнера.
    file_encryption_key: str = ""

    # Уведомления персоналу на телефон — веб-пуш кабинета (spec 0050, issue #477).
    # `fake` — память процесса (ADR-007: pytest, uvicorn с хоста); `vapid` —
    # настоящие службы пуша, compose задаёт его явно. При `vapid` без ключа
    # пуша нет совсем: кнопки на телефоне нет, рассылка молчит.
    staff_push_backend: Literal["vapid", "fake"] = "fake"
    # Приватный ключ VAPID — 32 байта в base64url (`python -m
    # hospitality.tools.generate_vapid_key`). Секрет: docs/runbooks/secrets.md;
    # смена ключа обрывает все подписки, телефоны переподписываются сами при
    # открытии кабинета. Формат проверяет preflight — по той же причине, что
    # у `file_encryption_key`: ValidationError печатает значение.
    staff_push_vapid_private_key: str = ""
    # Контакт отправителя для служб пуша (RFC 8292 `sub`): mailto: или https:.
    staff_push_vapid_subject: str = "mailto:support@necturn.com"

    # Rate-limit гостевого чата (issue #41, spec 0023, §6): защита общего
    # LLM-бюджета тенанта (ERR-AI-002) от одного болтливого/злонамеренного чата.
    # Две ступени на chat_id: всплеск (N сообщений за окно) и дневной потолок
    # (UTC-сутки, окно не настраивается). Значение ≤0 отключает ступень.
    # Счётчики — Redis (shared/ratelimit.py, fail-open при его недоступности);
    # пер-тенантные значения — конфиг тенанта, Phase 1 (P-11).
    guest_chat_rate_limit_messages: int = 20
    guest_chat_rate_limit_window_seconds: int = 600
    guest_chat_rate_limit_messages_per_day: int = 200
    # Денежная ступень (issue #404, spec 0023 §5): доля дневного бюджета LLM
    # тенанта, которую один чат вправе потратить за UTC-сутки. Число сообщений
    # деньги не ограничивает — цена сообщения растёт с длиной текста и истории.
    # 0,1 — $1 из $10: обнулить отель смогут только 10+ чатов. ≤0 — ступень выключена.
    guest_chat_daily_budget_share: float = 0.1

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

    # Суперадмин (spec 0044): сессия на час — он заходит сделать дело, вход с
    # кодом из приложения, — и ссылка нового пароля на сутки: суперадмин
    # передаёт её сам, как приглашение, но доступ она даёт к готовой учётке.
    staff_superadmin_session_ttl_minutes: int = 60
    staff_password_reset_ttl_hours: int = 24

    # Rate-limit логина кабинета (spec 0033 §3.3, канон Redis-счётчика 0023):
    # два ключа на попытку — пара «отель + логин» (подбор пароля к одной
    # учётке, spec 0037 §3) и IP (перебор учёток с одного клиента). Тратят
    # бюджет только НЕУДАЧНЫЕ попытки (issue #207). Бюджеты разные, потому что
    # разный субъект: за учёткой стоит один человек, за IP — весь отель, ушедший в
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
    # Вторая ступень того же ввода — по IP (аудит 26.10 К-1, #483): ключ по
    # комнате не видит потока выдуманных комнат с одного адреса, а bcrypt
    # (~230 мс CPU) выполняется на каждую. Бюджет просторный, как у bind-ссылки
    # ниже: гости за NAT отеля делят адрес; тратится каждой попыткой, и
    # успешной тоже — bcrypt стоит одинаково. Окно — общее с ключом комнаты.
    guest_code_verify_ip_rate_limit_attempts: int = 60

    # Rate-limit'ы ссылки привязки — QR листка гостю (spec 0033 §6/§9, канон 0023).
    # Выпуск — по (tenant, stay): кнопки на карточке заселения; лимит ловит
    # залипший скрипт, а не человека. Привязка — по IP: у анонима с QR нет
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
    # Доля дневного LLM-бюджета тенанта, на которой алертер предупреждает
    # (ERR-OPS-006, issue #103): расход из `llm_daily_spend_usd` делится на
    # `llm_daily_budget_usd` — обе метрики отдаёт /metrics. 0.8 оставляет пятую
    # часть суток на реакцию: при ровном расходе порог переходится примерно за
    # 4–5 часов до исчерпания. Значение ≤0 или ≥1 отключает линию (страховочный
    # люк, как у гостевых лимитов): на 1.0 предупреждать уже не о чем — бот
    # к этому моменту молчит.
    alert_llm_budget_ratio: float = 0.8
    # Сколько вызовов провайдера LLM подряд должны провалиться (ошибкой или
    # исчерпанными таймаутами) у одного тенанта, чтобы алертер сказал «модель
    # не отвечает» (ERR-OPS-009, issue #374). Серия, а не окно времени: на
    # пилоте ночью сообщение в час, и любое окно было бы для ночи глухим или для
    # дня шумным. 3 — одиночный сбой среди успехов алерта не даёт, а пустой
    # баланс виден на третьем госте без ответа. 0 отключает линию (страховочный
    # люк — пока транзитные 429/529 неотличимы от пустого баланса, #409).
    # Отдельной настройки у ERR-OPS-010 (бюджет исчерпан) нет: первый же отказ
    # по бюджету — уже молчащий бот, порог там ничего не отфильтровал бы.
    alert_llm_failure_streak_threshold: int = Field(default=3, ge=0)
    # Свежесть бэкапа БД (issue #106). Каталог бэкапов ХОСТА, видимый алертеру
    # (в staging-стеке он смонтирован в контейнер только для чтения как
    # /backups); пусто — линия выключена, и это дефолт dev/CI, где каталога нет.
    # Порог 30 часов = суточный цикл cron (03:00 UTC, docs/runbooks/restore.md)
    # плюс 6 часов запаса: пропущенный ночной прогон виден тем же утром, а
    # медленный дамп или сдвиг расписания на час ложным алертом не станут.
    # Граница `gt=0` на самом поле (канон #137/#282): 0 или отрицательное
    # означало бы «любой бэкап устарел» — линия шумела бы непрерывно.
    alert_backup_dir: str = ""
    alert_backup_max_age_hours: float = Field(default=30.0, gt=0)
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


class InvalidFileEncryptionKeyError(ValueError):
    """`FILE_ENCRYPTION_KEY` задан, но это не 32 байта в base64. Значения в тексте нет."""


def file_encryption_key_bytes(settings: Settings) -> bytes | None:
    """Ключ шифрования файлов байтами; не задан — `None`, негоден — исключение без значения."""
    value = settings.file_encryption_key.strip()
    if not value:
        return None
    try:
        key = base64.b64decode(value, validate=True)
    except binascii.Error:
        key = b""
    if len(key) != 32:
        raise InvalidFileEncryptionKeyError("FILE_ENCRYPTION_KEY must be 32 bytes in base64")
    return key


# Порядок группы кривой P-256: приватный ключ — число от 1 до n-1 (SEC 1 §3.2.1).
_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


class InvalidStaffPushConfigError(ValueError):
    """Настройка веб-пуша кабинета негодна (spec 0050 §8). Значения ключа в тексте нет."""


def staff_push_vapid_key_bytes(settings: Settings) -> bytes | None:
    """Приватный ключ VAPID байтами; не задан — `None` (пуша нет), негоден — исключение.

    Проверяется и контакт `sub`: служба пуша Apple отвергает сообщения без
    `mailto:`/`https:` — лучше отказ старта, чем тихо не доходящие пуши.
    """
    subject = settings.staff_push_vapid_subject
    if not subject.startswith(("mailto:", "https://")):
        raise InvalidStaffPushConfigError(
            "STAFF_PUSH_VAPID_SUBJECT must start with mailto: or https://"
        )
    value = settings.staff_push_vapid_private_key.strip()
    if not value:
        return None
    try:
        key = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except binascii.Error:
        key = b""
    if len(key) != 32 or not 0 < int.from_bytes(key, "big") < _P256_ORDER:
        raise InvalidStaffPushConfigError(
            "STAFF_PUSH_VAPID_PRIVATE_KEY must be a P-256 private key: 32 bytes in base64url"
        )
    return key


@lru_cache
def get_settings() -> Settings:
    return Settings()
