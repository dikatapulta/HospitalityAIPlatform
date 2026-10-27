# Spec 0047: хранилище файлов — общий узел

- **Статус:** реализуется (PR #495)
- **Issue:** #491 — ТЗ и ответ основателя 27.10 в теле issue; решения сессии — строкой в
  [FOUNDER_REVIEW_QUEUE](../FOUNDER_REVIEW_QUEUE.md).
- **Дата:** 2026-10-27
- **Решение об устройстве:** [ADR-022](../adr/ADR-022-file-storage.md) — том на диске сервера,
  шифрование приложением, антивирус отложен.

## 1. Проблема

Room service (#418) принимает фото блюд и товаров, реквизиты для АВР (#493) — файлы гостя, фото к
заявке (#82) ждёт того же. Без общего узла каждое направление написало бы своё хранение, проверку
и бэкап — по-разному (P-12).

## 2. Решение

Модули зовут только сервис ядра `platform/files.py` и хранят у себя id строки `uploaded_files`.
Сервис проверяет содержимое, шифрует, кладёт файл в хранилище через порт и пишет метаданные.
Экранов загрузки здесь нет — они у направлений.

## 3. Контракты

**Порт** `shared/ports/file_storage.py` — `FileStoragePort`: `put(key, data, content_type)`,
`get(key) -> bytes`, `delete(key)`, `check() -> bool`. Ключ — `{tenant_id}/{purpose}/{uuid}.{ext}`;
формат проверяется до адаптера (ключ с `..` — `ValueError`). `get` отсутствующего —
`StoredObjectNotFoundError`, `delete` отсутствующего — не ошибка. Адаптеры: `fake` (память) и
`local` (каталог `FILE_STORAGE_ROOT`, файлы 0600, запись атомарна). Адаптер кладёт в порт только
composition root (`app.py`, `worker.py`).

**Таблица** `uploaded_files` (миграция 0036, RLS по канону 0002): `id, tenant_id, purpose,
storage_key, content_type, size_bytes, sha256, original_name, uploaded_by_user_id NULL,
guest_identity_id NULL, created_at`. `guest_identity_id` без FK — ядро не ссылается на таблицы
модулей. Колонки `deleted_at` нет: удаление жёсткое (файл и строка), след — лог `file_deleted`.

**Сервис** `platform/files.py` (в контексте тенанта):

| Функция | Что делает |
|---|---|
| `store_upload(purpose, data, original_name, *, uploaded_by_user_id, guest_identity_id) -> UploadedFileRead` | проверка → шифрование → `put` → строка; упала строка — файл удаляется |
| `read_file(file_id) -> StoredFile` | метаданные + расшифрованные байты; чужой или нет — ERR-FILES-004 |
| `delete_file(file_id)` | файл, затем строка |
| `delete_expired_files(purpose, *, created_before) -> int` | ретеншн: файл, затем строка (повтор доделает) |
| `file_response(stored) -> Response` | HTTP-ответ с заголовками §5 |

`original_name` — без пути и управляющих символов, расширение — от проверенного содержимого
(«фото.jpg», ставшее WebP, — «фото.webp»).

## 4. Что принимаем

Тип — по байтам: `%PDF-` → PDF; zip с `word/document.xml` → DOCX; иначе Pillow обязан
декодировать JPEG, PNG или WebP (другие декодеры Pillow выключены — EPS через Ghostscript не
откроется). Расширение имени и `Content-Type` клиента не читаются. Проверка идёт в дочернем
процессе (`platform/file_sandbox.py`, FOUNDATION §11, ADR-022): пустое окружение, лимиты памяти и
CPU, таймаут 30 с (истёк — ERR-FILES-001), не больше двух одновременно; ребёнок умер без ответа —
ERR-FILES-006.

| Назначение | Типы | Картинка → WebP, сторона ≤ | Срок |
|---|---|---|---|
| `menu_photo` | фото | 960 px | пока живёт позиция меню (удаляет #418) |
| `request_photo` | фото | 960 px | срок текстов гостя (§6) |
| `guest_document` | фото, PDF, DOCX | 1600 px | срок текстов гостя (§6) |

- Фото реквизитов — 1600 px, а не 960: цифры БИН и ИИК на 960 px не читаются ни глазом, ни моделью.
- Лимиты: фото — 5 МБ, PDF и DOCX — 10 МБ (ERR-FILES-001, 413); больше 40 Мп — тоже 001.
- Картинка перекодируется без метаданных: EXIF (геолокация телефона), XMP, ICC не уезжают;
  поворот из EXIF применяется до этого. Не декодировалась — ERR-FILES-002 (415).
- SVG, HTML, исполняемые, произвольный zip — ERR-FILES-002. DOCX с `vbaProject.bin` или
  macro-enabled типом — ERR-FILES-003 (415). PDF и DOCX хранятся как есть и здесь не разбираются.
- Шифрование: AES-256-GCM ключом `FILE_ENCRYPTION_KEY`, AAD — ключ объекта; формат — версия
  (1 байт) + nonce (12) + шифртекст. Ключа нет — ERR-FILES-005 (503).

## 5. Выдача

`file_response`: `X-Content-Type-Options: nosniff` и `Content-Security-Policy: default-src 'none';
sandbox` на всё; PDF и DOCX — `Content-Disposition: attachment` с именем (RFC 6266, UTF-8);
картинки — без него, на страницах — `<img>` своим маршрутом направления с проверкой тенанта и
права. `guest_document` — `Cache-Control: no-store`, остальное — `private, max-age=3600`.

## 6. Ретеншн

Срок и его владелец — [spec 0032](0032-guest-text-retention.md) §2: файлы, присланные гостем
(`guest_document`, `request_photo`), живут столько же, сколько его тексты, от загрузки. Джоба
`channels/common/retention.py` зовёт `delete_expired_files` по обоим назначениям; счётчик
`guest_files_deleted` — в логе `guest_text_retention_completed`. `menu_photo` джоба не трогает.

## 7. Наблюдаемость

- Логи: `file_stored`, `file_rejected` (причина), `file_deleted`; сбои — WARNING, не ERROR:
  `file_storage_failed` и `file_sandbox_failed` (006), `file_decrypt_failed` и
  `file_encryption_key_missing` (005). ERROR на пути запроса уносит тело запроса — файл — в
  Sentry, пока открыт #433. `original_name` в логи не пишется (PII).
- Негодный `FILE_ENCRYPTION_KEY` валит старт в preflight; значение в сообщение не попадает.
- Метрика `file_operations_total{operation, outcome, reason}`.
- `/health/ready` — проверка `file_storage` (том на месте и доступен на запись).
- Коды `ERR-FILES-001…006` — [errors.md](../runbooks/errors.md).

## 8. Эксплуатация

- Том `files_data` в `ops/docker-compose.yml` и `ops/deploy/docker-compose.staging.yml` у `app` и
  `worker`; `FILE_STORAGE_BACKEND=local`, `FILE_STORAGE_ROOT`, `FILE_ENCRYPTION_KEY` — в
  `environment:` обоих. Каталог в образе создаётся с владельцем `appuser`.
- `backup.sh`: архив тома одноразовым контейнером образа → `age` → `<метка>.files.tar.age` рядом
  с дампом, тот же ретеншн; сбой — сбой бэкапа. `make backup-fetch` забирает архив с дампом.
  Восстановление — [restore.md](../runbooks/restore.md).
- Ключ: staging — заводит PR, включающий первый приём файлов; гостевая копия — по
  [pilot-copy.md](../runbooks/pilot-copy.md). Копия ключа — в менеджере паролей основателя.

## 9. Тесты

- Контракт порта на обоих адаптерах (`tests/test_file_storage_port.py`): put/get, перезапись,
  нет ключа, повтор delete, ключ с `..` и абсолютный — отказ; права 0600 у диска.
- Содержимое (`tests/test_file_content.py`): PNG с EXIF → WebP ≤ 960 без EXIF; JPEG с GPS и
  поворотом → 1600 px без EXIF; текст под `.jpg`, SVG, HTML, exe → отказ; DOCX с
  `vbaProject.bin` и docm → отказ; 11 МБ → отказ; бомба 64 Мп → отказ; шифр привязан к ключу.
- Сервис на живом Postgres (`tests/test_files.py`): шифртекст на томе; ключ несёт тенант; чужой
  тенант не читает и не удаляет по id; RLS на сыром SQL, UPDATE, DELETE и INSERT; отказ ничего не
  оставляет; без ключа — 005; строка без файла и смерть песочницы — 006; заголовки выдачи.
- Песочница (`tests/test_file_content.py`): настоящий дочерний процесс — WebP, PDF из байтов
  родителя, отказ с причиной; preflight — негодный ключ без значения в сообщении.
- Ретеншн (`channels/common/tests/test_retention.py`): файл и строка гостя уходят, `menu_photo`
  остаётся. `backup.sh` (`tests/test_pre_migrate_backup.py`): архив тома рядом с дампом;
  сбой архива останавливает деплой.

## Не входит

S3-адаптер; антивирус (ADR-022); превью PDF; приём файлов из Telegram (#82); экраны загрузки и
маршруты выдачи — у направлений (#418, #493).
