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
Скрипт ставит Docker, создаёт пользователя `deploy` (в группе docker), каталог
`/opt/hospitality`, открывает в firewall SSH и порт 8000.

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

## Часть D. Диагностика

| Симптом | Что смотреть |
|---|---|
| Job `deploy-staging` пропущен (skipped) | Не задан `STAGING_SSH_HOST` — см. A5 |
| Падает шаг «Настроить SSH» / scp / ssh | Неверный `STAGING_SSH_KEY`/`HOST`/`USER`; ключ не в `authorized_keys` (A3) |
| `pull` не тянет образ / первый деплой красный | Пакет GHCR не Public (A7) или нет логина под приватным |
| `up --wait` таймаут | `docker compose -f docker-compose.staging.yml --env-file .env logs` на сервере |
| smoke `/health/ready` == 503 | Postgres/Redis не поднялись; смотреть логи db/redis; проверить `.env` |
| Стек не пережил перезагрузку | Проверить `restart: unless-stopped` и `systemctl status docker` |

Ручные команды на сервере (в `/opt/hospitality`):
```bash
docker compose -f docker-compose.staging.yml --env-file .env ps
docker compose -f docker-compose.staging.yml --env-file .env logs -f app
```

## Ограничения Phase 0 (осознанный долг)
- **HTTP без TLS** и порт наружу. TLS/reverse-proxy (Caddy/Traefik) и домен —
  вместе с продакшеном в Phase 1 (§11 «TLS везде» — требование прода).
- Бэкапы Postgres и репетиция восстановления — Task 0019, отдельно.
- Один VPS. Мульти-хост/managed-БД — по мере роста (§10.12).
