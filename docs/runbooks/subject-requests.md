# Runbook: обращения субъектов персональных данных (гостей)

> Пакет П1.6 (issue #126). Права субъекта по Закону № 94-V: доступ,
> исправление, блокирование, удаление, отзыв согласия, возражение против
> автоматизированной обработки (ст. 19-1). **Срок ответа — 3 рабочих дня**
> (консервативно применяем ко всем типам; для ст. 19-1 он прямо установлен —
> юраудит 22.07). Исполняет ответственное лицо (приказ —
> `docs/legal/responsible-person-order-template.md`).
>
> Фаза 0/пилот: ручная SQL-процедура на сервере. Staff-инструмент — Фаза 1+.

## 1. Приём и верификация

1. Каналы приёма: ресепшен отеля (передаёт платформе ≤ 1 рабочего дня — DPA
   N.5.3) или контакт оператора из политики конфиденциальности.
2. **Верификация — через отель**: гость обращается на ресепшен, отель
   подтверждает личность заселённого гостя (это его штатная компетенция) и
   передаёт: имя, номер комнаты, период проживания, канал (Telegram/веб).
   Дистанционное обращение без подтверждения отеля не исполняется на
   удаление/выдачу (риск отдать чужую переписку) — только консультация.
3. Каждое обращение — строка в журнале обращений (файл у ответственного
   лица): дата, тип, субъект, чем закончилось, дата ответа.

## 2. Найти данные гостя (общая часть)

Все команды — на прод-сервере (мультиплексированный SSH), от имени владельца
БД (RLS не препятствует; поэтому **каждый запрос обязан содержать явные
условия идентификации** — работаем в транзакции и проверяем счётчики перед
записью):

```bash
# sh -c в одинарных кавычках обязателен: POSTGRES_* живут внутри контейнера db,
# на хосте их нет — без экранирования psql получит пустые -U/-d (канон restore.md)
docker compose -f docker-compose.staging.yml --env-file .env \
  exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Диалоги гостя — два пути:

```sql
-- Путь А: Telegram (гость знает свой чат / отель знает chat_id из заявки)
SELECT id, channel, external_id FROM conversations
WHERE channel = 'telegram' AND external_id = '<chat_id>';

-- Путь Б: веб-чат — через проживание (комната + период от отеля)
SELECT c.id, c.channel, g.display_name, s.room_number
FROM stays s
JOIN guests g            ON g.id = s.guest_id
JOIN guest_sessions gs   ON gs.stay_id = s.id
JOIN conversations c     ON c.guest_identity_id = gs.guest_identity_id
WHERE s.room_number = '<room>'
  AND s.check_in_at >= '<check_in_date>'::timestamptz - interval '1 day';
```

Найденные `conversations.id` — вход для процедур ниже (`<conv_ids>`).
Если найдено несколько диалогов — сверить с отелем, прежде чем продолжать.

## 3. Процедуры по типам обращений

### 3.1. Доступ / копия данных

```sql
\copy (SELECT direction, created_at, text FROM messages
       WHERE conversation_id IN (<conv_ids>) ORDER BY created_at)
  TO '/tmp/subject-export.csv' CSV HEADER;
```

`\copy` пишет внутрь контейнера db — забрать на хост и подчистить оба места:

```bash
docker compose -f docker-compose.staging.yml exec -T db \
  cat /tmp/subject-export.csv > subject-export.csv
docker compose -f docker-compose.staging.yml exec db rm /tmp/subject-export.csv
```

Плюс заявки (через `request_origins`): `summary`, `details`, статус, даты.
Файл передать гостю через отель (печать/письмо), **копии на сервере и хосте
удалить сразу после передачи**.

### 3.2. Исправление

Единственные исправимые поля — `guests.display_name` (пишет персонал при
заселении) и связка комнаты: правится через отель (перезаселение в кабинете /
UPDATE по согласованию). Тексты сообщений не редактируются — только удаление.

### 3.3. Удаление (и отзыв согласия)

Отзыв согласия = прекращение обработки: выполняем блокирование (3.4) +
удаление по запросу. В транзакции:

```sql
BEGIN;
-- 0. Посмотреть объём (санити-чек: цифры совпадают с ожиданием?)
SELECT count(*) FROM messages WHERE conversation_id IN (<conv_ids>);

-- 1. Переписка
DELETE FROM messages WHERE conversation_id IN (<conv_ids>);

-- 2. Свободный текст заявок этого гостя — обезличить (агрегаты и статусы
--    остаются: операционная история отеля, лиц не идентифицирует)
UPDATE service_requests SET
    summary = '[удалено по обращению субъекта]',
    details = NULL,
    resolution_note = NULL
WHERE id IN (SELECT request_id FROM request_origins
             WHERE conversation_id IN (<conv_ids>));

-- 3. Имя гостя (если запрошено полное удаление; связка Stay остаётся
--    обезличенной записью проживания)
UPDATE guests SET display_name = NULL
WHERE id IN (SELECT s.guest_id FROM stays s
             JOIN guest_sessions gs ON gs.stay_id = s.id
             JOIN conversations c ON c.guest_identity_id = gs.guest_identity_id
             WHERE c.id IN (<conv_ids>));

-- 4. Сами диалоги — ПОСЛЕДНИМ шагом (шаги 2–3 ходят через эти строки):
--    conversations.external_id (chat_id / веб-идентификатор) — тоже ПД
--    («идентификатор», закон № 231-VIII); каскадом уйдут остатки
--    request_origins и messages.
DELETE FROM conversations WHERE id IN (<conv_ids>);
COMMIT;
```

Записи о согласии (`guest_sessions.consent_at/_version`) **не удаляются** —
доказательство того, что обработка была законной (PII_REGISTRY). По той же
причине не удаляются строки `guest_identities`: на них каскадом висят
`guest_sessions` с записями согласия; идентификатор канала без переписки и
имени идентифицирует только сам факт данного согласия.

В ответе гостю указать: данные удалены из рабочей базы; в зашифрованных
резервных копиях они исчезают по мере плановой ротации — не позднее 14 дней
(`BACKUP_RETENTION_DAYS`); из копий бэкапы не восстанавливаются, кроме аварии,
и при восстановлении удаление воспроизводится по журналу обращений.

### 3.4. Блокирование

Приостановить обработку без удаления — погасить доступ канала:

```sql
UPDATE guest_sessions SET revoked_at = now()
WHERE guest_identity_id IN
  (SELECT guest_identity_id FROM conversations WHERE id IN (<conv_ids>));
UPDATE stay_access_codes SET revoked_at = now()
WHERE stay_id IN (SELECT stay_id FROM guest_sessions gs
                  JOIN conversations c ON c.guest_identity_id = gs.guest_identity_id
                  WHERE c.id IN (<conv_ids>));
```

Для Telegram дополнительно: новые сообщения гостя остаются без LLM-обработки
после включения consent-gate (#127 — отозванное согласие закрывает gate);
до #127 — зафиксировать в журнале и исполнить удалением (3.3).

### 3.5. Возражение против автоматизированной обработки (ст. 19-1)

Гость вправе получать обслуживание без ИИ: ответ — «обслуживание через
ресепшен отеля» (телефон — в статическом ответе канала), диалог блокируется
по 3.4, открытые заявки гостя доводит персонал через staff-канал. Отдельный
режим «человек в чате» — Фаза 1+ (handoff), не обещать его в ответе.

## 4. Ответ субъекту

В течение 3 рабочих дней с момента обращения (в т.ч. промежуточный «приняли,
исполняем», если объём большой): что сделано, что осталось в обезличенном
виде и почему (операционная история отеля), судьба резервных копий. Ответ —
через тот канал, откуда пришло обращение (обычно отель). Копия — в журнал
обращений.
