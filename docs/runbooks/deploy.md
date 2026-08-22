# Runbook: деплой на staging

> Task 0006. Цель: «деплой — рутина, а не событие» (FOUNDATION §10.11, план правило 5).
> Этот runbook позволяет **поднять staging с нуля** на новом сервере и объясняет,
> как код попадает на staging при каждом merge в `main`.

## Как это устроено (одна картинка словами)

```
git push → merge в main
      │
      ▼
GitHub Actions (.github/workflows/ci.yml)
  check ─┐
         ├─(оба зелёные)→ deploy-staging:
  dev-env┘     1. docker build --target production
               2. push образа в GHCR (ghcr.io/<owner>/hospitality-app:<sha>)
               3. scp compose+deploy.sh на VPS
               4. ssh: deploy.sh <образ>  →  pull, alembic upgrade head,
                                             up --wait, smoke /health/ready
      │
      ▼
   VPS (staging): docker compose со стеком app+db+redis
```

**Ключевое решение канона:** на сервере крутится готовый образ из GHCR, а не сборка
на месте. Сервер «тупой» — ему нужны только Docker, `.env` с секретами и compose-файл;
он запускает ровно тот артефакт, что прошёл CI. Откат = деплой прежнего тега.

Рассмотренные альтернативы (почему не они):
- **git pull + build на сервере** — сервер связан со сборкой (медленнее, нужны
  build-зависимости), и бежит не тот артефакт, что тестировали.
- **docker save | ssh docker load** — без реестра и приватно, но нет истории тегов
  и отката, полный образ гонится каждый деплой.

---

## Часть A. Поднять staging с нуля (разовое, ~20 минут)

### A1. Создать VPS
- Любой провайдер (Hetzner, Timeweb, PS.kz и т.п.), Ubuntu 22.04/24.04, 1–2 vCPU / 2 ГБ RAM.
- Резидентность данных РК — вопрос **продакшена** (§11, отдельный ADR), для staging некритично.
- Записать публичный IP → это `STAGING_SSH_HOST`.

### A2. Прогнать bootstrap на сервере
Скопировать и запустить [ops/deploy/bootstrap-server.sh](../../ops/deploy/bootstrap-server.sh):
```bash
scp ops/deploy/bootstrap-server.sh root@<IP>:/root/
ssh root@<IP> "DEPLOY_USER=deploy bash /root/bootstrap-server.sh"
```
Скрипт ставит Docker и `age` (шифрование бэкапов, issue #81), создаёт
пользователя `deploy` (в группе docker), каталог `/opt/hospitality`, открывает в
firewall SSH и порт 8000.

### A3. Ключ деплоя для CI
Сгенерировать **отдельную** пару ключей только для деплоя (не личный ключ):
```bash
ssh-keygen -t ed25519 -f deploy_key -N "" -C "github-actions-staging"
```
- Публичный `deploy_key.pub` → на сервер:
  ```bash
  ssh root@<IP> "mkdir -p /home/deploy/.ssh && \
    cat >> /home/deploy/.ssh/authorized_keys && \
    chown -R deploy:deploy /home/deploy/.ssh && chmod 600 /home/deploy/.ssh/authorized_keys" < deploy_key.pub
  ```
- Приватный `deploy_key` → в GitHub-секрет `STAGING_SSH_KEY` (см. [secrets.md](secrets.md)).
- Удалить локальные копии ключа после переноса.

### A4. Секреты на сервере (`.env`)
```bash
scp ops/deploy/.env.staging.example deploy@<IP>:/opt/hospitality/.env
ssh deploy@<IP>
nano /opt/hospitality/.env      # задать сильный POSTGRES_PASSWORD (openssl rand -hex 24)
```
`.env` живёт только на сервере и в репозиторий не попадает (§11).
Обязательно заполнить `BACKUP_AGE_RECIPIENT` — публичный ключ age основателя:
без него бэкапы БД не создаются вовсе (issue #81, [restore.md](restore.md),
раздел «Шифрование») **и деплой не доходит до миграций** — он останавливается на
снимке БД перед ними (issue #135, часть B).

### A4b. Постоянный HTTPS-вход — именованный Cloudflare-туннель (issue #65)
Разовая настройка на сервере (нужен домен в Cloudflare, напр. `necturn.com`):
```bash
ssh deploy@<IP>
CF=~/cloudflared-cli                       # CLI-бинарник в хоуме deploy (НЕ в /opt/hospitality:
                                           # там имя cloudflared занято директорией конфига)
$CF tunnel login                           # печатает URL → открыть в браузере, выбрать зону, Authorize
$CF tunnel create hospitality-staging      # создаёт туннель + секретный <UUID>.json в ~/.cloudflared/
$CF tunnel route dns hospitality-staging staging.necturn.com   # DNS CNAME создаётся автоматически
```
`tunnel create` печатает id туннеля — он **уже** прописан в
[ops/deploy/cloudflared/config.yml](../../ops/deploy/cloudflared/config.yml)
(`tunnel:`). Если создаёшь новый туннель с другим id — обнови там же.
`tunnel create` кладёт JSON с правами `0400` (только владелец). Процесс в
контейнере cloudflared работает под другим UID — дать ему чтение:
```bash
chmod 644 ~/.cloudflared/<UUID>.json    # иначе cloudflared: permission denied → restart loop
```
В `.env` задать путь к этому JSON:
```
CLOUDFLARED_CREDS_FILE=/home/deploy/.cloudflared/<UUID>.json
PUBLIC_BASE_URL=https://staging.necturn.com
```
`PUBLIC_BASE_URL` теперь читает и приложение: из неё собирается ссылка на
политику конфиденциальности в тексте согласия гостя (spec 0029). Неверный адрес
= нерабочая ссылка в юридически обязательном тексте — проверяется открытием
`$PUBLIC_BASE_URL/legal/privacy`.
Дальше вход поднимает `docker-compose.staging.yml` (сервис `cloudflared`), а
`setWebhook` на каждом деплое делает `deploy.sh` — руками ничего не нужно.
> Access-политику на хост `staging.necturn.com` **не вешать**: Telegram не умеет
> логиниться, вебхук упрётся в экран входа. Нужен открытый вход с TLS — туннель
> даёт его сам.

### A5. GitHub-секреты
Repo → Settings → Secrets and variables → Actions → New repository secret:
`STAGING_SSH_HOST`, `STAGING_SSH_USER` (`deploy`), `STAGING_SSH_KEY`, при нестандартном
порте — `STAGING_SSH_PORT`. Полный список и смысл — [secrets.md](secrets.md).
Как только `STAGING_SSH_HOST` задан, job `deploy-staging` перестаёт пропускаться.

### A6. Первый деплой — создаёт образ в GHCR
Запусти деплой вручную: Actions → CI → Run workflow (ветка `main`) или `make deploy-staging`.
Этот прогон соберёт production-образ и **запушит** его в GHCR — так впервые появляется
пакет **`hospitality-app`** (`ghcr.io/<owner>/hospitality-app`). Новый пакет GHCR по
умолчанию **Private**, поэтому шаг деплоя на сервере (`pull`) на этом первом прогоне
**упадёт (job красный) — это ожидаемо**: пакет ещё приватный, серверу нечем логиниться.
Пакет теперь существует — переходи к A7.

### A7. Сделать пакет GHCR Public
Чтобы «тупой» сервер тянул образ без логина, у пакета должна быть видимость **Public**
(простейший путь для staging): GitHub → профиль/организация → Packages →
`hospitality-app` → Package settings → Change visibility → **Public**.
(Код репозитория публичный, но видимость пакета — отдельная настройка.)
> Когда образ станет чувствительным — оставить пакет Private и класть на сервер
> read-only PAT: `docker login ghcr.io` под ним в `/opt/hospitality` (в `.env`, не в репозиторий).
> Тогда шаг A7 не нужен, а первый деплой (A6) не покраснеет.

### A8. Перезапустить деплой
Пакет теперь Public — запусти деплой ещё раз (Actions → Run workflow или `make deploy-staging`).
Серверный `pull` пройдёт, `up --wait` поднимет стек, post-deploy smoke `/health/ready` даст
зелёный job. Дальше деплой идёт сам при каждом merge в `main`.

### A9. Проверить
```bash
curl http://<IP>:8000/health/live     # {"status":"ok"}
curl http://<IP>:8000/health/ready    # 200 + статусы postgres/redis
```

---

## Часть B. Обычный деплой (рутина)

Ничего делать не нужно: **merge PR в `main` → CI зелёный → изменение на staging**.
Ручной перезапуск того же кода — кнопкой «Run workflow» или `make deploy-staging`.

### Миграции БД при деплое (Task 0009)

Перед перезапуском приложения `deploy.sh` применяет миграции новым образом:
`compose run --rm --no-deps app alembic upgrade head` (миграции и `alembic.ini`
входят в production-образ; БД staging не открыта наружу, поэтому только так до
неё и можно достать). Старая версия приложения в этот момент ещё обслуживает
трафик — миграции в рамках одного деплоя обязаны быть обратно-совместимыми
(добавить таблицу/колонку — да; удалить/переименовать — только в два деплоя).

**Снимок БД перед миграцией** (issue #135). Прямо перед `alembic upgrade`
деплой зовёт канон бэкапа [ops/backup/backup.sh](../../ops/backup/backup.sh) с
меткой `pre-migrate-<первые 12 символов git sha образа>`; в логе это пара строк
`==> Снимок БД перед миграцией: метка pre-migrate-<sha>` и
`==> OK: бэкап создан, проверен и зашифрован: …/pre-migrate-<sha>-<метка>.dump.age`.
Снимок шифруется и стареет ровно как ночной бэкап. **Не снялся — деплой
останавливается до миграции** (fail-closed): без страховки упавшая миграция
откатывалась бы из ночного дампа с потерей до суток данных. «Не снялся» —
это про файл, а не про код возврата `backup.sh`: устаревший `backup.sh` на
сервере (до #135) метку игнорирует и кладёт дамп под именем ночного бэкапа,
и деплой встанет так же. Откат по снимку — [restore.md](restore.md), случай В.

Аварийный обход — только осознанно и на один прогон, когда фикс нужнее
страховки (например, сломано само шифрование бэкапов, а на staging чинить
надо сейчас):
```bash
SKIP_PRE_MIGRATE_BACKUP=1 ./deploy.sh ghcr.io/<owner>/hospitality-app:<sha>
```
В логе останется строка `ВНИМАНИЕ: снимок БД перед миграцией ПРОПУЩЕН`. В CI
этого рычага нет и быть не должно: он для человека у консоли.

### Проверка конфигурации на старте контейнера (issue #267)

У образа есть ENTRYPOINT — `ops/entrypoint.sh`. Он прогоняет
`python -m hospitality.preflight` (список fail-fast проверок старта: годность
значений в `Settings`, `LLM_MODEL` по прайс-листу) и только потом `exec`'ает
команду контейнера. Проверка провалилась — контейнер выходит с кодом 1 и ошибкой
конфигурации в логах; деплой краснеет сразу, а не ждёт healthcheck до таймаута.

Вид ошибки зависит от проверки, и это важно при чтении логов: `LLM_MODEL` вне
прайс-листа даёт **одну строку** («…отсутствует в прайс-листе…»), а граница
значения в `Settings` — **traceback pydantic** на десяток строк, где нужное
лежит в конце: `validation error for Settings`, имя поля и нарушенная граница.
Под `restart: unless-stopped` стена повторяется каждые несколько секунд —
смотреть надо последние строки, а не первые.

Это касается **каждой** команды образа, включая `alembic upgrade head` и сид в
`deploy.sh`: негодная конфигурация останавливает деплой ДО изменения схемы БД.
`command:` в compose ENTRYPOINT не отменяет — он переопределяет только CMD.

**Обойти проверку разово** — только для аварийных команд, когда `.env` заведомо
негоден, а команду выполнить надо (восстановление из бэкапа,
[restore.md](restore.md) шаг 5):

```bash
docker compose -f docker-compose.staging.yml --env-file .env \
  run --rm --no-deps --entrypoint alembic app upgrade head
```

`--entrypoint` подменяет ENTRYPOINT образа целиком, поэтому preflight не идёт, а
аргументы команды пишутся после имени сервиса. В штатном деплое так делать
нельзя: он для того и падает, чтобы негодная конфигурация не доехала до БД.

Одно исключение из «каждой команды» записано в самом compose — и оно не в
проверке, а в том, какие значения до сервиса доходят: preflight `alerter`
проходит наравне со всеми, но `LLM_MODEL` ему намеренно не пробрасывается,
поэтому негодная модель в `.env` его не роняет — watchdog обязан пережить
негодную конфигурацию приложения, иначе некому будет о ней сообщить. Ширина
защиты ровно такая: `LOG_LEVEL` алертеру пробрасывается и границу несёт
(`Literal`), так что негодный уровень логирования уложит и watchdog тоже.

Почему отдельным шагом, а не проверкой внутри приложения: она роняет процесс, в
котором исполняется, а `uvicorn --workers N` (CMD production-образа) и
`--reload` (цель `dev`) держат над рабочим процессом родителя-супервизора.
Родитель `SystemExit` ребёнка не наследует: замер на #267 — 8 перезапусков детей
за 25 с при живом родителе и живом контейнере.

### Сквозная проверка конвейера событий (Task 0010)

Вместе с приложением на staging работает сервис `worker` (тот же образ,
`python -m hospitality.worker`). Проверить конвейер «публикация → outbox →
воркер → подписчик» после деплоя:

```bash
ssh deploy@<IP>
cd /opt/hospitality
docker compose -f docker-compose.staging.yml exec app \
  python -m hospitality.tools.publish_demo_event
# в выводе — лог demo_event_published с correlation_id; затем:
docker compose -f docker-compose.staging.yml logs worker | grep <correlation_id>
# ожидаемые события: event_delivered и canary_echoed с тем же correlation_id
```

## Часть C. Откат

Последний рабочий образ записан строкой `APP_IMAGE=...` в `/opt/hospitality/.env`
(её ведёт `deploy.sh`, руками не трогать). Откат на конкретную версию
(тег = git sha коммита):
```bash
ssh deploy@<IP>
cd /opt/hospitality
./deploy.sh ghcr.io/<owner>/hospitality-app:<старый-sha>
```
`deploy.sh` перезапишет `APP_IMAGE` в `.env` только после успешного smoke, так что
повторный `./deploy.sh` без аргумента всегда поднимает последнюю *здоровую* версию.

**Схему откат не опускает** — `deploy.sh` умеет только `upgrade head`. Обычно это
безразлично: миграции обратно-совместимы в рамках одного деплоя (см. комментарий
в самом скрипте), и старый код живёт на новой схеме. Исключения перечислены здесь,
и для них откат образа — половина дела:

| Откат на образ старше | Что ещё выполнить | Что будет, если забыть |
|---|---|---|
| #298 (миграция `0025`, источник заявки) | `docker compose -f docker-compose.staging.yml run --rm --no-deps app alembic downgrade 0024` | Старый код не умеет заполнять `service_requests.origin`, а умолчания у колонки больше нет — **создание заявок падает целиком**, гость получает ошибку на каждое обращение |

## Часть D. Диагностика

| Симптом | Что смотреть |
|---|---|
| Job `deploy-staging` пропущен (skipped) | Не задан `STAGING_SSH_HOST` — см. A5 |
| Падает шаг «Настроить SSH» / scp / ssh | Неверный `STAGING_SSH_KEY`/`HOST`/`USER`; ключ не в `authorized_keys` (A3) |
| `pull` не тянет образ / первый деплой красный | Пакет GHCR не Public (A7) или нет логина под приватным |
| `up --wait` таймаут | `docker compose -f docker-compose.staging.yml --env-file .env logs` на сервере |
| smoke `/health/ready` == 503 | Postgres/Redis не поднялись; смотреть логи db/redis; проверить `.env` |
| Стек не пережил перезагрузку | Проверить `restart: unless-stopped` и `systemctl status docker` |
| Заявки не создаются после отката образа, в логах `null value in column "origin"` | Схема осталась от новой версии — опустить её: см. таблицу исключений в части C |
| `app` и `worker` падают сразу после старта, в логах `LLM_MODEL=… отсутствует в прайс-листе` | Модель в `.env` не из прайс-листа `MODEL_PRICING_USD_PER_MTOK` (`ai/gateway/service.py`): поправить `LLM_MODEL` (id без даты) или добавить цену новой модели в прайс-лист — issue #137 |
| Контейнеры падают сразу, в логах `validation error for Settings` и имя поля | Значение в `.env` вне допустимых границ (например `LLM_MAX_ATTEMPTS=0` при `ge=1`): в сообщении названы поле и нарушенная граница — поправить `.env` и повторить деплой |
| Деплой встал на шаге `alembic upgrade head`, миграции не применились | То же: ENTRYPOINT образа проверяет конфигурацию перед любой командой (см. Часть B). Это защита, а не поломка — схема БД не тронута; чинить `.env` |
| Деплой встал на шаге «Снимок БД перед миграцией» | Схема БД не тронута (issue #135). Смотреть строку выше в логе: нет `BACKUP_AGE_RECIPIENT`/`age` — [restore.md](restore.md), «Шифрование»; не отвечает БД — логи `db`. Крайняя мера — `SKIP_PRE_MIGRATE_BACKUP=1` (Часть B) |
| `backup.sh завершился без ошибки, но снимка … не появилось` | На сервере устаревший `backup.sh` (до #135): метку он игнорирует, дамп лёг под именем ночного бэкапа. Обновить файл — `scp ops/backup/backup.sh deploy@<IP>:/opt/hospitality/` (в CI это делает scp-шаг деплоя, ручной `./deploy.sh` — нет) |

Ручные команды на сервере (в `/opt/hospitality`):
```bash
docker compose -f docker-compose.staging.yml --env-file .env ps
docker compose -f docker-compose.staging.yml --env-file .env logs -f app
```

## Ограничения Phase 0 (осознанный долг)
- **HTTP без TLS** и порт наружу. TLS/reverse-proxy (Caddy/Traefik) и домен —
  вместе с продакшеном в Phase 1 (§11 «TLS везде» — требование прода).
- Один VPS. Мульти-хост/managed-БД — по мере роста (§10.12).

Бэкапы Postgres и восстановление (в т.ч. «сервер потерян целиком») — Task 0019:
[restore.md](restore.md). Smoke-приёмка после деплоя — `make smoke-staging`
(tests/smoke/README.md).
