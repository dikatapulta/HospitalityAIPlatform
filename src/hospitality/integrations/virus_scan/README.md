# integrations/virus_scan — адаптеры антивируса

Реализации порта `VirusScanPort` (`shared/ports/virus_scan.py`,
[spec 0075](../../../../docs/specs/0075-guest-file-antivirus.md)). Подключает только composition
root — `app.py` зовёт `build_virus_scanner(settings)` и кладёт результат в порт
(`set_virus_scanner`). Ядро и модули адаптеры не импортируют (контракт 2 import-linter);
пользуется им `platform/file_antivirus.py` — для PDF и DOCX, которые узел файлов хранит
как присланы.

## Что умеет

`scan(data) -> ScanResult` — вердикт о байтах; `check()` — отвечает ли антивирус. Нет
вердикта — `VirusScanUnavailableError`, никогда не «чисто».

| Адаптер | Файл | Для чего |
|---|---|---|
| `FakeVirusScanAdapter` | `fake.py` | тесты, CI, локальный uvicorn (ADR-007): тестовый файл EICAR заражён, как у ClamAV; `add_signature` — своя «сигнатура» подстрокой; `unavailable = True` — антивирус лежит |
| `ClamdVirusScanAdapter` | `clamd.py` | контуры: демон clamd сервиса `clamav` по TCP, протокол `INSTREAM`, куски по 64 КБ |

## Конфигурация

| Переменная | Значение |
|---|---|
| `VIRUS_SCAN_BACKEND` | `fake` (умолчание кода и локального compose) или `clamav` (умолчание `docker-compose.staging.yml`) |
| `CLAMAV_HOST`, `CLAMAV_PORT` | адрес clamd; умолчание `clamav:3310` — имя сервиса в сети compose |

Ключей нет: clamd слушает только сеть compose, порт наружу не открыт. Базы сигнатур
обновляет freshclam внутри контейнера — нужен исходящий доступ к `database.clamav.net`.

## Лимиты

- Таймаут вызова — 30 с; дольше — `VirusScanUnavailableError` (ERR-FILES-008).
- Файл больше `StreamMaxLength` clamd (у ClamAV 1.5.4 10 МБ проходят, 120 МБ — нет) clamd
  обрывает с `ERROR`; узел файлов дальше 10 МБ не пропускает.
- Память сервиса `clamav` — 1,2 ГБ, до 2,4 ГБ при обновлении баз (документация ClamAV);
  поэтому он под профилем compose `antivirus` (spec 0075 Р-6).

## Health и метрики

- `check()` — `zPING` → `PONG`; Fake — `not unavailable`. В `/health/ready` не входит
  (spec 0075 §4); проверка руками — [pilot-copy.md](../../../../docs/runbooks/pilot-copy.md),
  «Документы гостя для АВР».
- Метрика `file_operations_total{operation="store", outcome, reason}` пишется вызывающим
  (`platform/file_antivirus.py`): `rejected/infected`, `error/antivirus`.

## Тесты

Один контрактный набор на оба адаптера — `tests/test_virus_scan_port.py` (R-7): clamd
говорит с тестовым сервером протокола, вердикт которому выносит Fake, — ответы обязаны
совпасть. Настоящий clamd в CI не поднимается; протокол сверен с ClamAV 1.5.4 руками.
