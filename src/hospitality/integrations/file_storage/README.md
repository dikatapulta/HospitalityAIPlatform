# integrations/file_storage — адаптеры хранилища файлов

Реализации порта `FileStoragePort` (`shared/ports/file_storage.py`,
[spec 0047](../../../../docs/specs/0047-file-storage.md), [ADR-022](../../../../docs/adr/ADR-022-file-storage.md)).
Подключает только composition root — `app.py` и `worker.py` зовут
`build_file_storage(settings)` и кладут результат в порт (`set_file_storage`).
Ядро и модули адаптеры не импортируют (контракт 2 import-linter).

## Что умеет

Хранит непрозрачные байты по ключу `{tenant_id}/{purpose}/{uuid}.{ext}`:
`put`, `get`, `delete`, `check`. О тенантах, типах и шифровании не знает — это
работа сервиса `platform/files.py`; адаптер получает уже зашифрованные байты.

| Адаптер | Файл | Где лежит | Для чего |
|---|---|---|---|
| `FakeFileStorageAdapter` | `fake.py` | память процесса | тесты, CI, локальный uvicorn (ADR-007) |
| `LocalDiskFileStorageAdapter` | `local_disk.py` | каталог `FILE_STORAGE_ROOT` — том `files_data` в compose | staging, гостевая копия, прод |

Дисковый: файлы 0600, каталоги 0700; запись атомарна (временный файл +
`os.replace`); ввод-вывод — в потоке, цикл событий не блокируется; ключ сверяется
с форматом и с корнем — `..` и абсолютный путь не дойдут до диска.

## Конфигурация

| Переменная | Значение |
|---|---|
| `FILE_STORAGE_BACKEND` | `fake` (умолчание кода) или `local` (compose задаёт сам) |
| `FILE_STORAGE_ROOT` | каталог дискового адаптера; в контейнере — `/var/lib/hospitality/files` |

Ключей у адаптеров нет. Ключ шифрования (`FILE_ENCRYPTION_KEY`) читает сервис,
а не адаптер — [secrets.md](../../../../docs/runbooks/secrets.md).

## Лимиты

Лимиты размера и типов — у сервиса (spec 0047 §4). Дисковый ограничен местом на
диске сервера: диск полон — `OSError`, сервис отвечает ERR-FILES-006.

## Health и метрики

- `check()` — корень существует (создаётся при первом вызове) и доступен на
  запись; его зовёт `/health/ready` (`file_storage`). Fake всегда `True`.
- Метрика `file_operations_total{operation, outcome, reason}` пишется сервисом
  вокруг вызовов порта — одна на все адаптеры.

## Тесты

Один контрактный набор на оба адаптера — `tests/test_file_storage_port.py`
(R-7). Новый адаптер (S3 при переезде в РК, ADR-022) — строкой в `ADAPTERS`
этого файла: не прошёл контракт — дефект адаптера.
