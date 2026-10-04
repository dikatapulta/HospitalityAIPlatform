# Runbook: гостевая копия (пилот) на staging-сервере

> Временный контур для живых гостей пилотного отеля — решение основателя
> 29.09.2026, отступление от [ADR-006](../adr/ADR-006-hosting-data-residency-kz.md)
> (раздел «Временное отступление») до переезда в РК (#49). Этапы пилота — #384.

## Что это и где живёт

Вторая копия стека на том же сервере, что staging (Hetzner, `deploy@<IP>` —
[deploy.md](deploy.md)). Код тот же, образ тот же из GHCR; своё — база, Redis,
секреты, адрес и расписание обновлений.

| Что | Staging | Гостевая копия |
|---|---|---|
| Каталог | `/opt/hospitality` | `/home/deploy/hospitality-pilot` (в `/opt` у `deploy` нет прав, `sudo` нет) |
| Compose-проект (префикс контейнеров) | `hospitality` | `hospitality-pilot` |
| Адрес | `staging.necturn.com` | `app.necturn.com` — туннель `hospitality-pilot` |
| Данные | синтетика | живые гости пилотного отеля |
| Обновление | само, на каждый merge в `main` (CI) | только по слову основателя, руками (ниже) |
| Ночной бэкап | cron 03:00 | cron 03:15 с путями копии — строка в «[Ночной бэкап](#ночной-бэкап)» |
| Telegram-бот | есть | **нет** (решение основателя 29.09): персонал работает в кабинете |

Код отеля (slug) — только в `.env` копии (`TELEGRAM_TENANT_SLUG`), в
репозиторий не пишется (решение основателя 20.09). Гостевой адрес —
`https://app.necturn.com/g/<код-отеля>/<номер>`, кабинет —
`https://app.necturn.com/staff`.

**Без Telegram-бота** эскалации («🚨 ЧП», «гостю нужен сотрудник», «ИИ
недоступен») адресата не имеют: подписчик пишет в лог `ERR-TELEGRAM-002` и
помечает событие доставленным. Гостя о ЧП текст перехвата отправляет звонить на
ресепшен (spec 0034 §3); при сбое ИИ гость читает «уже зову сотрудника» — звать
некому (#393). Алерты основателю идут отдельным алерт-ботом
(`TELEGRAM_ALERT_*`) и работают, но с префиксом «[staging]» — см. «Известные
отличия».

## Обновить копию (только по слову основателя)

Ставится версия, которую основатель уже проверил на staging.

```bash
ssh deploy@<IP>
cd /home/deploy/hospitality-pilot
# 1. Скрипты и compose-файл CI обновляет только в /opt/hospitality — взять оттуда:
cp /opt/hospitality/deploy.sh /opt/hospitality/backup.sh /opt/hospitality/docker-compose.staging.yml .
# 2. Образ — тот, что сейчас на staging (строка APP_IMAGE= в /opt/hospitality/.env):
grep '^APP_IMAGE=' /opt/hospitality/.env
./deploy.sh ghcr.io/dikatapulta/hospitality-app:<sha>
```

`deploy.sh` снимает шифрованный снимок базы копии перед миграцией, применяет
миграции, поднимает стек и проверяет `/health/ready`. Снаружи:
`curl -s -o /dev/null -w '%{http_code}' https://app.necturn.com/health/ready` → `200`.
Лучшее время — днём между заездом и выездом. **Обновление на образ с #399** —
переход персонала на логины: войти по email после него не сможет никто,
порядок — «Первый менеджер кабинета» ниже.

**Откат** — [deploy.md](deploy.md), часть C, в каталоге копии:
`./deploy.sh ghcr.io/dikatapulta/hospitality-app:<прежний-sha>`. Восстановление
базы из снимка — [restore.md](restore.md), случай В, с путями копии. Откат ниже
образа, записавшего в конфиг тенанта новое поле, молча лишает бота справочника:
старый образ поля не знает и не читает конфиг целиком (#387). У копии такое
поле — `reception_room_dial` с `5907bcc`; до #387 перед таким откатом ключ
снимают из `tenants.config`.

## Ночной бэкап

Строка в `crontab -l` пользователя `deploy`. Все три переменные обязательны: у
`backup.sh` `COMPOSE_FILE` и `ENV_FILE` по умолчанию смотрят в
`/opt/hospitality`, и строка с одним `BACKUP_DIR` каждую ночь клала бы в каталог
копии дамп базы **staging**. Алерт ERR-OPS-008 проверяет только свежесть файлов
в каталоге и промолчал бы.

```
15 3 * * * COMPOSE_FILE=/home/deploy/hospitality-pilot/docker-compose.staging.yml ENV_FILE=/home/deploy/hospitality-pilot/.env BACKUP_DIR=/home/deploy/hospitality-pilot/backups /home/deploy/hospitality-pilot/backup.sh >> /home/deploy/hospitality-pilot/backups/backup.log 2>&1
```

Проверка — как у staging ([restore.md](restore.md), «Разовая настройка
расписания»): свежий `backups/hospitality-*.dump.age` и строка `OK: бэкап
создан…` в конце `backups/backup.log`. Копии вне сервера у этих бэкапов нет:
`make backup-fetch` забирает только staging (#391).

## Отель в копии

Заведён 29.09 онбордингом ([tenant-onboarding.md](tenant-onboarding.md)) с
профилем `ops/onboarding/pilot-hotel.json`, внутри контейнера копии:

```bash
docker compose -f docker-compose.staging.yml --env-file .env run --rm --no-deps -T app \
    python -m hospitality.tools.onboard_tenant ops/onboarding/pilot-hotel.json \
    --slug <код-отеля> --reception-phone "<мобильный ресепшена>" --reception-room-dial 0
```

Повторный запуск идемпотентен: справочник и чаты служб он переносит из прежнего
конфига всегда, телефон и цифру ресепшена (`0` — в тексте ЧП, spec 0034 §3) —
если их флаг не передан. В базе копии цифра записана 29.09, повтором после
обновления на `5907bcc`.

**Справочник отеля** загружен 29.09 из локального файла основателя
(`ops/onboarding/*.local`, в репозиторий и в образ не попадает) через
`platform.config.mutate_tenant_config`: страницы «Справочник отеля» (#335) ещё
нет. Правка до неё — тем же примитивом, скриптом через
`docker compose … run --rm --no-deps -T app python -` со stdin.

**Первый менеджер кабинета** — `tools/staff_bootstrap` без `-T` (пароль
вводит основатель, getpass), из каталога копии и её compose-файлом — команда
шага 5 [tenant-onboarding.md](tenant-onboarding.md) с `/opt/hospitality/…`
отправила бы её в контейнер staging:

```bash
cd /home/deploy/hospitality-pilot
docker compose -f docker-compose.staging.yml --env-file .env exec app \
    python -m hospitality.tools.staff_bootstrap <ЛОГИН> --name "Имя" --tenant-slug <код-отеля>
```

Дальше персонал приглашается из кабинета. **После обновления копии на образ с
#399** email-учётки войти больше не могут: менеджер переходит путём (а) или (б)
шага 5 рунбука онбординга (для (б) — команда выше), остальных он отключает и
приглашает заново с логином — в «Сотрудниках» они помечены «без логина». Их
открытые сессии работают до своего срока, поэтому переприглашать лучше в тот же
день, а не когда человек упрётся в форму входа.

## Известные отличия от прода по ADR-006

- Сервер в Германии: база с данными граждан РК вне РК (ст. 12) — то самое
  отступление; выход — #49.
- Общий сервер со staging: SSH-ключ CI (`deploy`, группа `docker`) достаёт и до
  базы копии.
- Общие со staging `ANTHROPIC_API_KEY`, `SENTRY_DSN`, ключ age бэкапов, алерт-бот
  ([secrets.md](secrets.md), раздел 2а).
- `SENTRY_ENVIRONMENT: staging` зашит в compose (#389): ошибки копии в Sentry
  помечены как staging, а её алерты приходят тем же алерт-ботом в тот же чат,
  что у staging, с тем же префиксом «[staging]». Алерт «[staging] …» может быть
  и про живой отель: пока #389 открыт, проверять оба контура — `docker compose
  ps` в `/opt/hospitality` и в каталоге копии, `health/ready` обоих адресов.
  `deploy.sh` пишет в конце «staging здоров» — про копию тоже.
- `deploy.sh` гоняет сид: в базе копии есть демо-тенант `demo-hotel` с
  минимальным демо-конфигом (город, пояс, язык), без персонала и заселений —
  гость без кода привязки получает отказ без вызова модели.

## Переезд в РК (после пилота, #49)

Креды туннеля `hospitality-pilot` работают только на одном сервере за раз. Два
`cloudflared` с одними кредами — реплики одного туннеля: Cloudflare делит
запросы между ними, и гости попадали бы то в старую базу, то в новую. Поэтому
новый сервер получает креды последним, а старый к этому моменту уже не
принимает запросы.

1. Сервер в РК по ADR-006 (`bootstrap-server.sh`, [deploy.md](deploy.md) часть A)
   со своими секретами — пока без кредов туннеля `hospitality-pilot`.
2. Здесь, в каталоге копии, остановить приём:
   `docker compose -f docker-compose.staging.yml --env-file .env stop cloudflared app worker`.
   С этого шага до шага 5 гостевой адрес не отвечает — делать в тихий час;
   алертер заалертит, это ожидаемо.
3. Снять свежий бэкап — строкой cron из «[Ночной бэкап](#ночной-бэкап)» без
   расписания, руками. Ночной не годится: всё, что гости и персонал записали
   после 03:15, в нём нет, а шаг 7 сотрёт это окончательно.
4. Восстановить этот дамп на новом сервере по [restore.md](restore.md), случай А:
   шаг 1, шаг 3 (вариант «том потерян»), шаги 4–5, шаг 6 — только `up -d --wait`.
   В команде шага 4 — IP нового сервера, дамп — из `backups/` копии. Сервер
   после части A уже работает, а его база создана миграциями его образа —
   обычно новее образа копии. Без шага 1 воркер пишет в базу во время
   `pg_restore`; без шага 3 таблицы новее дампа остаются, и `alembic upgrade
   head` на шаге 5 падает. `curl` из шага 6 restore.md смотрит на staging —
   здесь его заменяет шаг 6 ниже.
5. Креды туннеля и `cloudflared/config.yml` копии — на новый сервер, поднять там
   `cloudflared`. Адрес `app.necturn.com` прежний, напечатанные QR работают.
6. Проверить не только `health/ready`: войти в кабинет
   `app.necturn.com/staff` и найти последнее заселение, сделанное до шага 2.
7. Только после этого здесь: `docker compose … down -v`, удалить каталог и
   бэкапы копии, убрать строку cron.

До шага 7 перенос откатывается: на новом сервере `stop cloudflared` и вернуть
его в состояние шага 1 — удалить там JSON кредов `hospitality-pilot`, в `.env`
вернуть прежний `CLOUDFLARED_CREDS_FILE`, в `cloudflared/config.yml` — прежний
конфиг. Одного `stop` мало: первый же `up -d` или `./deploy.sh` там поднимает
все сервисы, и с кредами пилота встала бы вторая реплика туннеля, пока копия
обслуживает гостей. Здесь —
`docker compose -f docker-compose.staging.yml --env-file .env up -d`; записанное
на новом сервере после шага 5 при этом не вернётся.

## Ротация кредов туннеля

Новый туннель — это новый id: CNAME `app.necturn.com` смотрит на старый, и без
правки DNS и `tunnel:` в конфиге копии адрес не поднимется. Гостевой адрес
лежит с шага 1 до шага 4 — делать в тихий час.

1. На сервере, в каталоге копии, остановить коннектор:
   `docker compose -f docker-compose.staging.yml --env-file .env stop cloudflared`.
   `tunnel delete` не удаляет туннель с живым соединением.
2. На Mac основателя (нужен `~/.cloudflared/cert.pem`; нет — `cloudflared
   tunnel login`, зона `necturn.com`):
   ```bash
   cloudflared tunnel delete hospitality-pilot    # жалуется на соединения — сначала tunnel cleanup hospitality-pilot
   cloudflared tunnel create hospitality-pilot    # печатает новый id, кладёт ~/.cloudflared/<новый-id>.json
   cloudflared tunnel route dns --overwrite-dns hospitality-pilot app.necturn.com
   ```
   `--overwrite-dns` обязателен: CNAME со старым id остаётся после `delete`.
3. Новый JSON — на сервер поверх `cloudflared/creds.json` копии с правами 644
   ([deploy.md](deploy.md) A4b); в `cloudflared/config.yml` копии строку
   `tunnel:` — на новый id. Путь в `.env` прежний.
4. Поднять коннектор:
   `docker compose -f docker-compose.staging.yml --env-file .env up -d cloudflared`
   и снаружи `curl -s -o /dev/null -w '%{http_code}' https://app.necturn.com/health/ready`
   → `200`. Старый `<id>.json` на Mac удалить.
