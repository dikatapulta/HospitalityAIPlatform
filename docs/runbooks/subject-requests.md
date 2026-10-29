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
4. Письмо гостя пришло на почту оператора — privacy@necturn.com или
   support@necturn.com (один ящик Zoho, ADR-006 §2; support@ открыт на
   necturn.com) — отвечать с того же адреса. Закрыв обращение, удалить
   переписку из ящика и адрес гостя из «Контактов» Zoho — и то и другое
   вместе с «Корзиной»: у «Контактов» она своя, удалённый контакт оттуда
   восстанавливается ещё до 30 дней. Политика (§2) обещает, что адрес и
   письмо живут только до закрытия. В «Контакты» адрес попадает сам — ответ
   добавляет туда получателя по умолчанию; один раз снять галочку
   «Automatically add recipients to Contacts» (Settings → Compose), и при
   закрытии останется только проверка.
   В журнале остаётся строка из п. 3, но **без адреса почты**: субъект — имя
   и комната от отеля, а без подтверждения отеля — пометка «по e-mail».

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

Плюс вопросы, на которые бот не нашёл ответа в справочнике отеля (пересказ
вопроса моделью — тоже текст гостя, PII_REGISTRY; появились с issue #334).
`\copy` — метакоманда psql, и аргументом она берёт только остаток **своей**
строки: команда обязана стоять одной строкой, перенос даёт `\copy: parse
error at end of line`, и файл не создаётся (блок выше страдает тем же — #347):

```sql
\copy (SELECT created_at, question FROM unanswered_questions WHERE conversation_id IN (<conv_ids>) ORDER BY created_at) TO '/tmp/subject-export-questions.csv' CSV HEADER
```

Этот файл тоже лежит внутри контейнера db, и его не удалят ни ретеншн, ни
ротация бэкапов — забрать и подчистить так же:

```bash
docker compose -f docker-compose.staging.yml exec -T db \
  cat /tmp/subject-export-questions.csv > subject-export-questions.csv
docker compose -f docker-compose.staging.yml exec db rm /tmp/subject-export-questions.csv
```

Плюс реквизиты для АВР ([spec 0049](../specs/0049-act-requisites.md)), если гость
присылал их формой со скрепкой. Поля и текст гостя лежат шифртекстом, файлы — на
томе шифртекстом: psql их не прочитает. Копию даёт бухгалтерия отеля со страницы
«Документы для АВР» — строки этого номера и дат, файлы скачиваются оттуда же.
Есть ли что отдавать:

```sql
SELECT a.created_at, a.room_number, count(f.id) AS files
FROM act_requests a LEFT JOIN act_request_files f ON f.act_request_id = a.id
WHERE a.anonymized_at IS NULL
  AND a.guest_identity_id IN (SELECT guest_identity_id FROM conversations
                              WHERE id IN (<conv_ids>))
GROUP BY a.id ORDER BY a.created_at;
```

Плюс заявки гостя: `summary`, `details`, статус, даты. Их id — из двух мест:
`request_origins` (заявки, оформленные ботом) и `conversation_escalations.request_id`
(карточки эскалаций — ЧП, «ИИ не ответил»: в их `summary` дословная реплика
гостя, а к диалогу они не привязаны, spec 0039 §2.7):

```sql
SELECT id, summary, details, status, created_at FROM service_requests
WHERE id IN (SELECT request_id FROM request_origins WHERE conversation_id IN (<conv_ids>)
             UNION
             SELECT request_id FROM conversation_escalations
             WHERE conversation_id IN (<conv_ids>) AND request_id IS NOT NULL);
```

Файл передать гостю через отель (печать/письмо), **копии на сервере и хосте
удалить сразу после передачи**.

### 3.2. Исправление

Единственные исправимые поля — `guests.display_name` (пишет персонал при
заселении) и связка комнаты: правится через отель (перезаселение в кабинете /
UPDATE по согласованию). Тексты сообщений не редактируются — только удаление.

### 3.3. Удаление (и отзыв согласия)

Отзыв согласия = прекращение обработки: выполняем блокирование (3.4) +
удаление по запросу.

Сначала — файлы гостя с тома (форма реквизитов для АВР, spec 0049): SQL объект на
томе не удалит, а порядок тот же, что у ретеншна, — объект, потом строка (spec
0047 §6). Список — в psql:

```sql
SELECT id, storage_key FROM uploaded_files
WHERE guest_identity_id IN (SELECT guest_identity_id FROM conversations
                            WHERE id IN (<conv_ids>));
```

Пусто — шаг пропустить. Иначе удалить объекты (каждый `storage_key` из списка) и
записать `id` строк — `<file_ids>` для шага 3б:

```bash
docker compose -f docker-compose.staging.yml --env-file .env exec app \
  rm -f /var/lib/hospitality/files/<storage_key_1> /var/lib/hospitality/files/<storage_key_2>
```

Затем в транзакции:

```sql
BEGIN;
-- 0. Посмотреть объём (санити-чек: цифры совпадают с ожиданием?)
SELECT count(*) FROM messages WHERE conversation_id IN (<conv_ids>);
SELECT count(*) FROM unanswered_questions WHERE conversation_id IN (<conv_ids>);

-- 1. Переписка
DELETE FROM messages WHERE conversation_id IN (<conv_ids>);

-- 2. Свободный текст заявок этого гостя — обезличить (агрегаты и статусы
--    остаются: операционная история отеля, лиц не идентифицирует). Оба
--    источника id: карточки эскалаций (spec 0039) к диалогу не привязаны и
--    находятся только через conversation_escalations — ДО шага 4, который
--    обнуляет их conversation_id (ON DELETE SET NULL).
UPDATE service_requests SET
    summary = '[удалено по обращению субъекта]',
    details = NULL,
    resolution_note = NULL
WHERE id IN (SELECT request_id FROM request_origins
             WHERE conversation_id IN (<conv_ids>)
             UNION
             SELECT request_id FROM conversation_escalations
             WHERE conversation_id IN (<conv_ids>) AND request_id IS NOT NULL);

-- 3. Имя гостя (если запрошено полное удаление; связка Stay остаётся
--    обезличенной записью проживания)
UPDATE guests SET display_name = NULL
WHERE id IN (SELECT s.guest_id FROM stays s
             JOIN guest_sessions gs ON gs.stay_id = s.id
             JOIN conversations c ON c.guest_identity_id = gs.guest_identity_id
             WHERE c.id IN (<conv_ids>));

-- 3б. Реквизиты для АВР (spec 0049): шифртексты и имя проверившего — стереть,
--     как это делает ретеншн (строка живёт: номер, даты, статус); строки
--     файлов, чьи объекты удалены выше (<file_ids>; не было — пропустить),
--     — удалить, связи act_request_files уйдут каскадом. Тоже ДО шага 4.
UPDATE act_requests SET
    guest_input_ciphertext = NULL,
    requisites_ciphertext = NULL,
    checked_by_display_name = NULL,
    anonymized_at = now()
WHERE guest_identity_id IN (SELECT guest_identity_id FROM conversations
                            WHERE id IN (<conv_ids>));
DELETE FROM uploaded_files WHERE id IN (<file_ids>);

-- 4. Сами диалоги — ПОСЛЕДНИМ шагом (шаги 2–3 ходят через эти строки):
--    conversations.external_id (chat_id / веб-идентификатор) — тоже ПД
--    («идентификатор», закон № 231-VIII); каскадом уйдут остатки
--    request_origins, messages и unanswered_questions.
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

### 3.6. Сотрудник: диалоги и оценки «Наставника»

Обращение сотрудника отеля, а не гостя ([spec 0048](../specs/0048-mentor.md) §9;
общая процедура для сотрудников — развилка #311). Подключение — как в §2. Логин в
базе записан с id отеля: `<tenant_id>:<ЛОГИН>`. Копия данных:

```sql
SELECT user_id FROM user_identities
WHERE kind = 'password' AND external_id = '<tenant_id>:<ЛОГИН>';  -- это <user_id>
SELECT c.mode, m.role, m.text, m.created_at FROM mentor_messages m
JOIN mentor_conversations c ON c.id = m.conversation_id
WHERE c.user_id = '<user_id>' AND c.tenant_id = '<tenant_id>' ORDER BY m.created_at;
SELECT mode, summary FROM mentor_conversations
WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>';
SELECT article_number, chapter_title, grade, created_at FROM mentor_quiz_answers
WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>' ORDER BY created_at;
```

Удаление — `DELETE FROM mentor_conversations WHERE user_id = '<user_id>' AND
tenant_id = '<tenant_id>';` (реплики уходят каскадом) и то же для
`mentor_quiz_answers`. `tenant_id` в условии обязателен: сотрудник может работать в
двух отелях, а владелец базы видит все (§2).

## 4. Ответ субъекту

В течение 3 рабочих дней с момента обращения (в т.ч. промежуточный «приняли,
исполняем», если объём большой): что сделано, что осталось в обезличенном
виде и почему (операционная история отеля), судьба резервных копий. Ответ —
через тот канал, откуда пришло обращение (обычно отель). Копия — в журнал
обращений: только текст ответа, без адреса и без цитаты письма гостя (§1
п. 4).
