# Runbook: обращения сотрудников отеля о своих персональных данных

> #311. Сотрудник отеля — субъект ПД по Закону № 94-V, как гость (гостей и посетителей
> витрины ведёт [subject-requests.md](subject-requests.md)). Срок ответа — тот же, 3 рабочих
> дня (там же, шапка); исправление — 1 рабочий день (ст. 25 п. 2 пп. 8). Исполняет ответственное
> лицо. Решение окна о том, что удаляется, а что нет, — запись
> [founder-review](../founder-review/2026-11-10-pr961-staff-subject-requests.md).

## 1. Приём и что положено

Отель — собственник базы с данными своих работников, платформа — оператор
([DPA](../legal/dpa-hotel-template.md) N.1 п. 4); обращение работника исполняется в порядке
и срок гостевого (DPA N.7).

- **Приём и верификация — через менеджера отеля:** работодатель знает работника, как ресепшен —
  заселённого гостя. Письмо сотрудника на privacy@ — ответить, что обращение передано
  работодателю, в тот же день передать его менеджеру и исполнять по его письменному указанию.
- **Журнал обращений** — тот же, что у гостей (subject-requests §1 п. 3).

| Просьба | Сотрудник работает | Уволен, учётка стёрта |
|---|---|---|
| Копия данных | §3 | §3 |
| Исправить имя | §4 | §4 |
| Удалить, отозвать согласие | «Наставник» и черновики записок — §5, §8; остальное — отказ с причиной (§6) | то же и обезличить историю (§7) |

Почему так (Закон № 94-V — редакция 14.09.2026, Цифровой кодекс № 255-VIII — редакция
23.07.2026, обе — zakon.uchet.kz):
- Удаления или обезличивания субъект вправе требовать «независимо от основания», кроме случаев,
  установленных законами (Цифровой кодекс, ст. 41 п. 1). Пока сотрудник работает, такой случай
  есть: согласие нельзя отозвать «при наличии неисполненного обязательства» (Закон № 94-V, ст. 8
  п. 2, ст. 24 п. 1 пп. 5) — трудовой договор исполняется, а без имени в «взял / закрыл /
  оплатил» работа в кабинете не идёт.
- После увольнения правоотношения прекращены, и данные подлежат удалению (ст. 18 пп. 2).
  Исключение — сохранение нужно для защиты прав третьих лиц (Цифровой кодекс, ст. 41 п. 4 пп. 1):
  заявка, по которой отель назвал открытый спор с гостем, и отправленная служебная записка или
  объяснительная в сроке хранения — в ней подписи и решения других работников.
- Отель сообщил об увольнении — §7 исполняется и без обращения (ст. 18 пп. 2); сам, без
  сообщения, — #964.

## 2. Найти сотрудника

Подключение и правила запросов — subject-requests §2. Логин в базе записан с id отеля:

```sql
SELECT u.id, u.display_name, u.status FROM users u
JOIN user_identities i ON i.user_id = u.id AND i.kind = 'password'
WHERE i.external_id = '<tenant_id>:<ЛОГИН>';                      -- id — это <user_id>
SELECT tenant_id, role_key, status FROM tenant_memberships WHERE user_id = '<user_id>';
```

Учётка без логина — по имени среди сотрудников отеля; найдено несколько — сверить с менеджером:

```sql
SELECT u.id, u.display_name, u.status FROM users u
JOIN tenant_memberships m ON m.user_id = u.id
WHERE m.tenant_id = '<tenant_id>' AND u.display_name ILIKE '%<фамилия>%';
```

`tenant_id` в условиях ниже обязателен: сотрудник может работать в двух отелях, а владелец базы
видит все.

## 3. Копия данных

Сотруднику — что хранится, зачем и сколько (ст. 24 п. 1 пп. 1): цели и сроки — его строки
в [PII_REGISTRY.md](../PII_REGISTRY.md), объём — выборкой ниже. Вывод — в файл: `\o
/tmp/staff-export.txt` перед запросами, `\o` после; забрать с контейнера и подчистить, как
в subject-requests §3.1. Эта же выборка — счётчики перед §7.

```sql
SELECT 'взял заявку' AS what, count(*), min(created_at), max(created_at) FROM service_requests
  WHERE tenant_id = '<tenant_id>' AND claimed_by_user_id = '<user_id>'
UNION ALL SELECT 'закрыл заявку', count(*), min(created_at), max(created_at) FROM service_requests
  WHERE tenant_id = '<tenant_id>' AND closed_by_user_id = '<user_id>'
UNION ALL SELECT 'отметил «Оплачено»', count(*), min(created_at), max(created_at) FROM service_requests
  WHERE tenant_id = '<tenant_id>' AND paid_by_user_id = '<user_id>'
UNION ALL SELECT 'заявка направлена ему', count(*), min(created_at), max(created_at) FROM service_requests
  WHERE tenant_id = '<tenant_id>' AND directed_to_user_id = '<user_id>'
UNION ALL SELECT 'шаг пути номера', count(*), min(created_at), max(created_at) FROM room_turnovers
  WHERE tenant_id = '<tenant_id>' AND '<user_id>' IN (reported_by_user_id, inspected_by_user_id,
    cleaning_started_by_user_id, cleaned_by_user_id, accepted_by_user_id, cancelled_by_user_id)
UNION ALL SELECT 'ответ на бронь столика', count(*), min(created_at), max(created_at) FROM table_bookings
  WHERE tenant_id = '<tenant_id>' AND answered_by_user_id = '<user_id>'
UNION ALL SELECT 'проверил реквизиты АВР', count(*), min(created_at), max(created_at) FROM act_requests
  WHERE tenant_id = '<tenant_id>' AND checked_by_user_id = '<user_id>'
UNION ALL SELECT 'строка журнала изменений', count(*), min(occurred_at), max(occurred_at) FROM staff_action_log
  WHERE tenant_id = '<tenant_id>' AND actor_user_id = '<user_id>'
UNION ALL SELECT 'этаж хаускипинга', count(*), min(assigned_at), max(assigned_at) FROM housekeeping_floor_assignments
  WHERE tenant_id = '<tenant_id>' AND user_id = '<user_id>'
UNION ALL SELECT 'приглашение', count(*), min(created_at), max(created_at) FROM staff_invites
  WHERE tenant_id = '<tenant_id>' AND accepted_user_id = '<user_id>'
UNION ALL SELECT 'устройство для подписи', count(*), min(created_at), max(created_at) FROM staff_passkeys
  WHERE user_id = '<user_id>'
UNION ALL SELECT 'телефон для уведомлений', count(*), min(created_at), max(created_at) FROM staff_push_subscriptions
  WHERE tenant_id = '<tenant_id>' AND user_id = '<user_id>';
```

Плюс «Наставник» — тексты целиком:

```sql
SELECT c.mode, m.role, m.text, m.created_at FROM mentor_messages m
JOIN mentor_conversations c ON c.id = m.conversation_id
WHERE c.user_id = '<user_id>' AND c.tenant_id = '<tenant_id>' ORDER BY m.created_at;
SELECT mode, summary, closed_at FROM mentor_conversations
WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>';
SELECT kind, summary, mark, mark_note, sent_at FROM mentor_ideas
WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>' ORDER BY created_at;
SELECT article_number, chapter_title, grade, created_at FROM mentor_quiz_answers
WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>' ORDER BY created_at;
SELECT chapter_title, question_count, pass_percent, correct_count, partial_count,
       wrong_count, started_at, finished_at, percent, passed FROM mentor_exam_attempts
WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>' ORDER BY started_at;
SELECT article_number, created_at FROM mentor_article_flags
WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>' ORDER BY created_at;
```

## 4. Исправление имени

```sql
UPDATE users SET display_name = '<верное имя>' WHERE id = '<user_id>';
```

Отметки в прошлых заявках и путях номеров — снимок имени на момент действия; по просьбе
исправить и их — те же `UPDATE` снимков, что в §7, со значением `'<верное имя>'` вместо `NULL`.
Логин не меняется ([spec 0037](../specs/0037-staff-hotel-login.md) §2).

## 5. «Наставник» — удаляется всегда

Диалоги, идеи и интервью ([spec 0071](../specs/0071-staff-ideas.md) §8 — уходят каскадом),
оценки «Тренировки», экзамены ([spec 0048](../specs/0048-mentor.md) §12) и отметки «Неточность
в статье» ([spec 0072](../specs/0072-mentor-exam-rollout.md) §6). Сверить счётчики с §3:

```sql
BEGIN;
DELETE FROM mentor_conversations WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>';
DELETE FROM mentor_quiz_answers WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>';
DELETE FROM mentor_exam_attempts WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>';
DELETE FROM mentor_article_flags WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>';
COMMIT;
```

## 6. Сотрудник работает: отказ в остальном

Имя и логин в учётке, отметки «взял / закрыл / оплатил / направлена», шаги пути номера, ответы
на брони, проверка реквизитов, журнал изменений и этажи — не удаляются. Мотивированный ответ
(ст. 25 п. 2 пп. 7) — §9.

## 7. Уволен: обезличить историю

Сначала:
- учётка стёрта — суперадмин, «Стереть учётку» в кабинете ([spec 0044](../specs/0044-superadmin.md)
  §5): гасит входы, членство, ключи подписи и подписки телефонов этого отеля; `users.status =
  'erased'` (§2);
- менеджер назвал заявки с открытым спором с гостем (номер `#N` и день) — их id через запятую
  встают вместо `<спорные>`; спора нет — пусто, `'{}'`. Спор закрыт — повторить блок с `'{}'`:

  ```sql
  SELECT id FROM service_requests
  WHERE tenant_id = '<tenant_id>' AND service_day = '<ГГГГ-ММ-ДД>' AND daily_number = <N>;
  ```

Затем в транзакции; число строк каждого `UPDATE` — как в §3:

```sql
BEGIN;
UPDATE service_requests SET claimed_by_display_name = NULL WHERE tenant_id = '<tenant_id>'
  AND claimed_by_user_id = '<user_id>' AND id <> ALL ('{<спорные>}'::uuid[]);
UPDATE service_requests SET closed_by_display_name = NULL WHERE tenant_id = '<tenant_id>'
  AND closed_by_user_id = '<user_id>' AND id <> ALL ('{<спорные>}'::uuid[]);
UPDATE service_requests SET paid_by_display_name = NULL WHERE tenant_id = '<tenant_id>'
  AND paid_by_user_id = '<user_id>' AND id <> ALL ('{<спорные>}'::uuid[]);
UPDATE service_requests SET directed_to_display_name = NULL WHERE tenant_id = '<tenant_id>'
  AND directed_to_user_id = '<user_id>' AND id <> ALL ('{<спорные>}'::uuid[]);
UPDATE room_turnovers SET reported_by_display_name = NULL WHERE tenant_id = '<tenant_id>' AND reported_by_user_id = '<user_id>';
UPDATE room_turnovers SET inspected_by_display_name = NULL WHERE tenant_id = '<tenant_id>' AND inspected_by_user_id = '<user_id>';
UPDATE room_turnovers SET cleaning_started_by_display_name = NULL WHERE tenant_id = '<tenant_id>' AND cleaning_started_by_user_id = '<user_id>';
UPDATE room_turnovers SET cleaned_by_display_name = NULL WHERE tenant_id = '<tenant_id>' AND cleaned_by_user_id = '<user_id>';
UPDATE room_turnovers SET accepted_by_display_name = NULL WHERE tenant_id = '<tenant_id>' AND accepted_by_user_id = '<user_id>';
UPDATE room_turnovers SET cancelled_by_display_name = NULL WHERE tenant_id = '<tenant_id>' AND cancelled_by_user_id = '<user_id>';
UPDATE table_bookings SET answered_by_display_name = NULL WHERE tenant_id = '<tenant_id>' AND answered_by_user_id = '<user_id>';
UPDATE act_requests SET checked_by_display_name = NULL WHERE tenant_id = '<tenant_id>' AND checked_by_user_id = '<user_id>';
UPDATE staff_action_log SET actor_login = NULL WHERE tenant_id = '<tenant_id>' AND actor_user_id = '<user_id>';
UPDATE staff_invites SET invited_name = 'Бывший сотрудник', login = NULL
  WHERE tenant_id = '<tenant_id>' AND accepted_user_id = '<user_id>';
DELETE FROM housekeeping_floor_assignments WHERE tenant_id = '<tenant_id>' AND user_id = '<user_id>';
COMMIT;
```

Последним — сама учётка, но только если нет активного членства в другом отеле (§2: имя видит и
он) и отправленных записок с его участием в сроке хранения (§8: имена на листе подписей берутся
из учётки). Есть — учётка ждёт; дату, когда истечёт последняя такая запись, — в журнал обращений,
и в эту дату шаг повторить:

```sql
BEGIN;
UPDATE users SET display_name = 'Бывший сотрудник' WHERE id = '<user_id>' AND status = 'erased';
UPDATE user_identities SET external_id = '<tenant_id>:~' || user_id
WHERE user_id = '<user_id>' AND kind = 'password' AND external_id LIKE '<tenant_id>:%';
COMMIT;
```

Логин заменён строкой, которую нельзя набрать во входе (знак `~` вне формата логина, spec 0037
§2): учётка показывается «без логина», логин освобождается — в истории его больше нет, и
к новому человеку она не перейдёт. Ключи подписи (`staff_passkeys`) остаются погашенными: их
открытым ключом проверяются подписи прошлых записок, а имени в них нет.

## 8. Служебные записки и объяснительные

[spec 0077](../specs/0077-staff-memos.md) §8. Тексты, замечания и образец подписи лежат
шифртекстом — psql их не прочитает. Копию текстов сотрудник берёт сам в кабинете: «Служебные
записки» → «Все» → запись → «Печать / сохранить в PDF». В ответ на обращение — перечень записей,
где он участник:

```sql
SELECT m.kind, m.number, m.status, s.role, s.response, s.method, s.signed_at, m.sent_at
FROM memo_signatures s JOIN memos m ON m.id = s.memo_id
WHERE s.user_id = '<user_id>' AND s.tenant_id = '<tenant_id>'
UNION ALL
SELECT kind, number, status, 'addressee', NULL, NULL, decided_at, sent_at FROM memos
WHERE addressee_id = '<user_id>' AND tenant_id = '<tenant_id>' AND status <> 'draft'
  AND decided_at IS NULL  -- решённые — уже строкой листа выше
ORDER BY sent_at;
SELECT count(*) AS views FROM memo_views WHERE user_id = '<user_id>' AND tenant_id = '<tenant_id>';
```

Удаление по обращению — только его черновики и образец подписи, в любой момент:
`DELETE FROM memos WHERE author_id = '<user_id>' AND tenant_id = '<tenant_id>' AND sent_at IS NULL;`
и `UPDATE memo_signature_samples SET revoked_at = now() WHERE user_id = '<user_id>' AND
tenant_id = '<tenant_id>' AND revoked_at IS NULL;` — погашенный образец без ссылок удалит ближайший
проход ретеншна, а на который ссылается лист записи, живёт сроком записи. Отправленную записку
или объяснительную по обращению одного участника не удалять и после увольнения: в ней подписи
и решения других работников, это документ Отеля (Цифровой кодекс, ст. 41 п. 4 пп. 1); уходит
по сроку хранения. Раньше — только по письменному указанию Отеля (оператор, DPA N.1).

Последняя отправленная запись с его участием — для шага учётки в §7 (пусто — записей нет; срок —
`memo_retention_days` отеля от этой даты):

```sql
SELECT max(m.sent_at) FROM memos m
WHERE m.tenant_id = '<tenant_id>' AND m.sent_at IS NOT NULL
  AND ('<user_id>' IN (m.author_id, m.addressee_id)
       OR m.id IN (SELECT memo_id FROM memo_signatures WHERE user_id = '<user_id>'));
```

## 9. Ответ сотруднику

В течение 3 рабочих дней, через менеджера: что сделано; что осталось и почему — пока работает,
отметки о работе нужны для исполнения трудового договора (ст. 8 п. 2), после увольнения —
только спорные заявки и записки в сроке, с датой, когда и они уйдут; в резервных копиях данные
исчезают по мере ротации — не позднее 14 дней (`BACKUP_RETENTION_DAYS`); тексты «Наставника»
провайдер модели держит в журналах злоупотреблений до 30 дней (spec 0048 §9). Копия — в журнал
обращений.
