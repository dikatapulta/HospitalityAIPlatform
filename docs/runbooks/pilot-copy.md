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
| Ночной бэкап | cron 03:00 | cron 03:15, `BACKUP_DIR` — `backups/` каталога копии |
| Telegram-бот | есть | **нет** (решение основателя 29.09): персонал работает в кабинете |

Код отеля (slug) — только в `.env` копии (`TELEGRAM_TENANT_SLUG`), в
репозиторий не пишется (решение основателя 20.09). Гостевой адрес —
`https://app.necturn.com/g/<код-отеля>/<номер>`, кабинет —
`https://app.necturn.com/staff`.

**Без Telegram-бота** эскалации («🚨 ЧП», «гостю нужен сотрудник», «ИИ
недоступен») адресата не имеют: подписчик пишет в лог `ERR-TELEGRAM-002` и
помечает событие доставленным. Гостя о ЧП текст перехвата отправляет звонить на
ресепшен (spec 0034 §3). Алерты основателю идут отдельным алерт-ботом
(`TELEGRAM_ALERT_*`) и работают.

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
Лучшее время — днём между заездом и выездом.

**Откат** — [deploy.md](deploy.md), часть C, в каталоге копии:
`./deploy.sh ghcr.io/dikatapulta/hospitality-app:<прежний-sha>`. Восстановление
базы из снимка — [restore.md](restore.md), случай В, с путями копии.

## Отель в копии

Заведён 29.09 онбордингом ([tenant-onboarding.md](tenant-onboarding.md)) с
профилем `ops/onboarding/pilot-hotel.json`, внутри контейнера копии:

```bash
docker compose -f docker-compose.staging.yml --env-file .env run --rm --no-deps -T app \
    python -m hospitality.tools.onboard_tenant ops/onboarding/pilot-hotel.json \
    --slug <код-отеля> --reception-phone "<мобильный ресепшена>"
```

Повторный запуск идемпотентен и переносит справочник, чаты служб и телефон.
Когда в образе появится `--reception-room-dial` (spec 0034 §3), тот же вызов с
`--reception-room-dial 0` добавит цифру в текст ЧП.

**Справочник отеля** загружен 29.09 из локального файла основателя
(`ops/onboarding/*.local`, в репозиторий и в образ не попадает) через
`platform.config.mutate_tenant_config`: страницы «Справочник отеля» (#335) ещё
нет. Правка до неё — тем же примитивом, скриптом через
`docker compose … run --rm --no-deps -T app python -` со stdin.

**Первый менеджер кабинета** — `tools/staff_bootstrap` через `exec app` без
`-T` (пароль вводит основатель, getpass), с `--tenant-slug <код-отеля>`; дальше
персонал приглашается из кабинета.

## Известные отличия от прода по ADR-006

- Сервер в Германии: база с данными граждан РК вне РК (ст. 12) — то самое
  отступление; выход — #49.
- Общий сервер со staging: SSH-ключ CI (`deploy`, группа `docker`) достаёт и до
  базы копии.
- Общие со staging `ANTHROPIC_API_KEY`, `SENTRY_DSN`, ключ age бэкапов, алерт-бот
  ([secrets.md](secrets.md), раздел 2а).
- `SENTRY_ENVIRONMENT: staging` зашит в compose: ошибки копии в Sentry помечены
  как staging. `deploy.sh` пишет в конце «staging здоров» — про копию тоже.
- `deploy.sh` гоняет сид: в базе копии есть демо-тенант `demo-hotel` (без
  конфига, персонала и заселений — гость без кода привязки получает отказ без
  вызова модели).

## Переезд в РК (после пилота, #49)

1. Сервер в РК по ADR-006 (`bootstrap-server.sh`, [deploy.md](deploy.md) часть A)
   со своими секретами.
2. Последний ночной бэкап копии → восстановить на новом сервере
   ([restore.md](restore.md)).
3. Креды туннеля `hospitality-pilot` и `cloudflared/config.yml` перенести на
   новый сервер: адрес `app.necturn.com` остаётся прежним, напечатанные QR
   продолжают работать.
4. Убедиться, что `app.necturn.com/health/ready` отвечает с нового сервера, и
   остановить копию здесь: `docker compose … down -v`, удалить каталог и
   бэкапы копии, убрать строку cron.
