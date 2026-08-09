# Runbook: алерты и наблюдаемость (Task 0018, FOUNDATION §10)

Как система сообщает о проблемах и что делать, когда пришёл алерт.

## Устройство (три механизма)

1. **Sentry (managed, §10.4, §10.12)** — каждая необработанная ошибка приложения
   и воркера становится событием с тэгами `tenant_id` и `correlation_id`.
   Включается переменной `SENTRY_DSN` (пустая = выключен). Смотреть:
   [sentry.io](https://sentry.io) → проект платформы → Issues (фильтр
   `environment:staging`).
2. **`GET /metrics` (§10.7)** — метрики Prometheus-формата: RED по эндпоинтам
   (`http_requests_total`, `http_request_duration_seconds`), глубина outbox
   (`outbox_pending_events` — очередь на доставку, `outbox_dead_letter_events` —
   похороненные, issue #133), LLM (`llm_calls_total`, `llm_tokens_total`,
   `llm_cost_usd_total` — по тенантам). Эндпоинт анонимный (явное решение §11,
   как `/health`). Быстрая проверка: `curl -s https://staging.necturn.com/metrics`
   (порт 8000 наружу закрыт Cloudflare-туннелем; изнутри сервера алертер ходит
   по docker-сети `http://app:8000`).
3. **Алертер (§10.8)** — сервис `alerter` staging-стека
   (`hospitality.tools.alerter`): раз в `ALERT_POLL_INTERVAL_SECONDS` (60 с)
   опрашивает `/health/ready` и `/metrics` приложения и шлёт алерты в
   Telegram-канал команды. Конфигурация — `TELEGRAM_ALERT_BOT_TOKEN` +
   `TELEGRAM_ALERT_CHAT_ID` в `.env` на сервере
   (ops/deploy/.env.staging.example).
4. **Воркер** — единственный, кто знает момент, когда доставка события сдалась
   окончательно, поэтому об этом (ERR-EVENTS-002) докладывает он сам, а не
   watchdog снаружи: раз в `WORKER_DEAD_LETTER_ALERT_INTERVAL_SECONDS` (60 с)
   шлёт в тот же канал команды одно сообщение на пачку похороненных событий
   (issue #133, ADR-015). Читает ту же пару `TELEGRAM_ALERT_*` — в сервисе
   `worker`, а не только `alerter`; пустая пара = шаг выключен (WARNING в логах).

Каждый алерт содержит: код ошибки, окружение, детали, тенанта (для
платформенных алертов — `platform`), correlation id (у внешнего опроса его
нет — `—`; у алерта воркера это correlation id публикации события) и ссылку на
этот runbook.

## ERR-OPS-001

**Алерт:** `/health/ready` недоступен или нездоров N опросов подряд.

1. Зайти на сервер: `ssh deploy@<IP>` (адрес и ключ — docs/runbooks/deploy.md).
2. Что лежит: `cd /opt/hospitality && docker compose -f docker-compose.staging.yml ps`
   — смотреть колонку STATUS у `db`, `redis`, `app`.
3. В тексте алерта есть `checks` из тела ready-ответа:
   - `postgres: error` → `docker compose -f docker-compose.staging.yml logs --tail 50 db`;
     чаще всего контейнер перезапущен/убит OOM или кончилось место (`df -h`).
   - `redis: error` → то же для `redis`.
   - `connection error` в деталях → приложение не отвечает вовсе:
     `docker compose -f docker-compose.staging.yml logs --tail 100 app`.
4. Поднять упавший сервис: `docker compose -f docker-compose.staging.yml up -d <service>`.
   После восстановления алертер сам пришлёт ✅.
5. Если причина неясна — Sentry Issues за последние минуты + логи app по времени алерта.

Ложный случай: деплой, который держит приложение недоступным дольше ~2 минут,
породит алерт и следом ✅ — это нормально и информативно.

## ERR-OPS-002

**Алерт:** всплеск 5xx (прирост ≥ порога за интервал опроса).

1. Открыть Sentry → Issues, отсортировать по последнему событию: каждый 5xx —
   событие с `correlation_id` и стеком. Это самый быстрый путь к причине.
2. Без Sentry: `docker compose -f docker-compose.staging.yml logs app | grep unhandled_error`
   — записи содержат `correlation_id`; полный след запроса —
   `grep <correlation_id>` по логам (docs/runbooks/errors.md, «Как найти ошибку в логах»).
3. Если всплеск начался сразу после деплоя — откат: `./deploy.sh <предыдущий образ>`
   (docs/runbooks/deploy.md).
4. Повторный алерт о продолжающемся всплеске придёт не раньше, чем через
   `ALERT_COOLDOWN_SECONDS` (15 мин) — тишина после первого алерта не означает,
   что всплеск кончился; смотреть метрики/Sentry.

## ERR-EVENTS-002

**Алерт:** событие outbox исчерпало повторы доставки и ушло в dead-letter.
Что это значит по существу: чья-то заявка не дошла до службы и сама уже не
дойдёт. Пока алерт не разобран, гость ждёт ответа, которого не будет.

1. Найти событие по id из алерта (первые 8 символов) — на сервере, в БД:
   ```sql
   SELECT id, event_name, tenant_id, correlation_id, attempts, dead_lettered_at, last_error
     FROM outbox_events WHERE id::text LIKE '<id из алерта>%';
   ```
   `last_error` в алерте обрезан до одной строки — здесь он целиком.
2. Полный след «публикация → попытки доставки» — по correlation id из алерта:
   `docker compose -f /opt/hospitality/docker-compose.staging.yml logs worker | grep <correlation_id>`
   (события `event_published`, `event_delivery_failed`, `event_delivery_exhausted`).
3. Сколько всего похоронено (алерт называет только свою пачку):
   `curl -s https://staging.necturn.com/metrics | grep outbox_dead_letter_events`.
   Растёт от алерта к алерту — сломан обработчик, а не одно событие.
4. Починить причину (обработчик или зависимость), затем вернуть события в
   очередь — SQL и объяснение полей в docs/runbooks/errors.md#err-events-002.
   Одно уведомление (не восстанавливая событие) быстрее отправить руками из
   staff-чата: гость ждёт минуты, а не деплой.
5. Повторного алерта об этих же событиях не будет: о каждом рассказывают ровно
   один раз (`dead_letter_alerted_at`). Тишина после алерта = «новых похорон не
   было», а не «разобралось само».

Ложный случай: во время сознательного эксперимента с недоступной зависимостью
(репетиция отказа, отключённый staff-чат) события хоронятся штатно — алерт
верен, разбирать нечего.

## Проверить тракт алертов вручную

С сервера (значения взять из `.env`):

```bash
curl -s "https://api.telegram.org/bot$TELEGRAM_ALERT_BOT_TOKEN/sendMessage" \
  -d chat_id="$TELEGRAM_ALERT_CHAT_ID" -d text="тест тракта алертов"
```

Сообщение в канале = бот и канал настроены верно. Полная репетиция DoD:
`docker compose -f docker-compose.staging.yml stop db` → алерт ERR-OPS-001 в
течение ~2–3 минут → `... start db` → ✅ о восстановлении.

## Как узнать chat.id канала

Добавить бота администратором канала → отправить в канал любое сообщение →
`curl -s "https://api.telegram.org/bot<токен>/getUpdates"` — в
`channel_post.chat.id` будет отрицательное число вида `-100…` — это и есть id.
Если `getUpdates` пуст — сначала `deleteWebhook` не делать (вебхук нужен
гостевому боту!): взять ОТДЕЛЬНОГО бота для алертов или посмотреть id через
пересылку сообщения боту @userinfobot.

## Известные ограничения (зафиксировано)

- Алертер живёт на том же VPS, что и приложение: смерть всего сервера не
  заалертит. Лечение — внешний managed uptime-сервис (§10.12), опрашивающий
  `https://staging.necturn.com/health/ready` (например, UptimeRobot, бесплатного
  тарифа достаточно). Рекомендуемый ручной шаг после Task 0018, вне её DoD.
- Состояние алертера — в памяти: перезапуск контейнера может повторить
  активный алерт один раз.
- Метрики процесса-воркера не экспонируются (у него нет HTTP): его здоровье
  видно через `outbox_pending_events` (растёт = воркер стоит или не успевает)
  и через Sentry (ERROR-логи воркера становятся событиями). Алерты воркер шлёт
  сам (ERR-EVENTS-002), но только о dead-letter — о собственной смерти он не
  доложит по построению (issue #136 в очереди: liveness воркера).
- Реестр Prometheus — на процесс: при нескольких uvicorn-воркерах `/metrics`
  отдаёт счётчики случайного воркера. Поэтому staging бежит с одним воркером
  (override `command` в docker-compose.staging.yml, Task 0019); multiprocess-
  реестр — Phase 1.

## Что дальше (Phase 1, не раньше)

- OpenTelemetry: `opentelemetry-instrumentation-{fastapi,sqlalchemy,redis,httpx}`,
  экспорт в managed-бэкенд (§10.12); процессор structlog, заполняющий
  `trace_id` из активного span (поле уже обязательно в каждой записи, §10.1);
  `init_tracing()` встанет рядом с `init_sentry()` в обоих composition root.
- Алерты «circuit breaker открыт», «превышение бюджета LLM», «SLA заявок
  нарушен» (§10.8) — по мере появления самих механизмов.
