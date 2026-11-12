# memos — служебные записки и объяснительные

## Назначение

Записка ходит по кабинету вместо бумаги: автор пишет её, отправляет подписантам, затем адресату,
и адресат принимает решение. Каждое действие под своим именем сотрудник подтверждает лицом или
отпечатком на своём устройстве либо паролем кабинета ([spec 0077](../../../../docs/specs/0077-staff-memos.md),
#936). Объяснительная — второй вид в тех же таблицах (§6): по требованию руководителя (фото
бумажного требования, «Получил(а)», срок два рабочих дня) или по своей инициативе; без
подписантов и решения.

Серия (§11): спека — подтверждение с устройства (`platform/staff_confirm.py`, PR 2) — **данные
модуля (PR 3, миграция 0071)** — кабинет (PR 4а — подпись до экранов, 4б — экраны:
`staff_portal/readme/memos.md`) — объяснительные (PR 5а — модуль, 5б — экраны) — «Оформить»
и экран шаблонов (PR 6).
Маршрутов и экранов в модуле нет; флаг `memos_enabled` проверяет кабинет.

## Состав

| Файл | Что даёт |
| --- | --- |
| `api.py` | Публичный интерфейс: единственная точка импорта извне (R-5) |
| `models.py` | Шесть тенантных таблиц (канон RLS); `MemoKind`, `MemoStatus`, `SignatureRole`, `SignatureResponse` |
| `schemas.py` | Ввод черновика и действий (`MemoDraftInput`, `ExplanationDraftInput` / `ExplanationText`, `SendIntent` / `SignIntent` / `RemarkIntent` / `DecisionIntent`, `PasskeyAnswer` / `PasswordAnswer`, `SignatureStrokes`), чтение (`MemoCard`, `MemoSummary`, `SignatureRead`, `DemandCard`, `DemandSummary`, `DemandState`), пределы длины |
| `access.py` | Кто видит запись и требование и кто может в них участвовать, кто вправе требовать объяснение; коды ERR-MEMO-001…003 |
| `codec.py` | Шифртексты ↔ значения, хэш содержимого, предмет подтверждения (и «Получил(а)») |
| `service.py` | Черновик записки, правка, отзыв, отправка адресату, «на основе», номер |
| `explanations.py` | Черновик объяснительной (по требованию или адресату), правка, «Дополнить» |
| `demands.py` | Требование объяснения: фото (`platform/files.py`, `memo_demand`), «Получил(а)» с подтверждением, карточка, вкладки, фото — участникам |
| `deadline.py` | Срок объяснения: конец второго рабочего дня после дня получения по поясу отеля |
| `signing.py` | Действия с подтверждением; образец подписи |
| `reading.py` | Карточка (проверка доказательств, журнал просмотров) и реестр по вкладкам |
| `retention.py` | Срок хранения — шаг 14 прохода `channels/common/retention.py` |
| `tests/` | Ход записки, ERR-MEMO-002 на каждом лишнем переходе, вторая вкладка, лист фиксируется отправкой, правка текста в базе ломает доказательство, только шифртекст в строках, ERR-MEMO-001 постороннему, менеджеру-неучастнику и суперадмину, RLS, номера при гонке, ретеншн; требование — «Получил(а)» только сотруднику, срок (выходные, праздник, пояс отеля), `senior` — только свои службы, подменённое требование ломает доказательство получения, фото уходит с требованием; объяснительная — после получения, без решения, «Дополнить», после срока |

## Публичный API

Все функции зовутся в `tenant_context` отеля (его ставит кабинет); актор — `StaffContext`.

- `create_draft(staff, MemoDraftInput) -> UUID`, `update_draft(staff, id, MemoDraftInput)` —
  черновик автора: шаблон отеля или свободная форма, заголовок до 200 знаков, текст до 6000,
  адресат — действующий `senior` или `manager`, подписантов до 10, ни автор, ни адресат.
  Нарушение участников или неизвестный шаблон — ERR-PLATFORM-002 (422, `details[0].reason`).
- `begin_action(staff, id, intent) -> ConfirmationRequest` → окно подтверждения в браузере →
  `complete_action(staff, id, intent, PasskeyAnswer | PasswordAnswer) -> MemoStatus`.
  Действия: `SendIntent` — автор из `draft` (номер «СЗ-2026/14», хэш содержимого; без
  подписантов — сразу `submitted`); `SignIntent` и `RemarkIntent` — подписант в `signing`, один
  ответ; `DecisionIntent` — адресат в `submitted`, комментарий обязателен у «Отказано» и «На
  доработку». Подтверждаемое — отель, запись, действие и SHA-256 содержимого (у замечания и
  решения — ещё их текст); доказательство — шифртекстом в строке листа. Запись изменилась между
  шагами — ERR-MEMO-002; образца нет — ERR-MEMO-003; отказ подтверждения — ERR-AUTH-017.
- `submit_to_addressee(staff, id)` — автор из `signing`: лист фиксируется, не ответившие
  остаются строкой без ответа. `withdraw(staff, id)` — автор из `draft` и `signing`, номер
  остаётся занятым. `create_based_on(staff, id) -> UUID` — новый черновик из `returned`.
- `get_memo(staff, id) -> MemoCard` — участнику: текст, лист с образцами и `proof_status`
  каждой строки (`verified` / `mismatch` / `password_only` / `disowned` — сходится, но ключ
  отвергнут владельцем «Это не я»), `content_intact`, просмотры, `based_on_number` («взамен
  СЗ-2026/14»); открытие отправленной пишет строку `memo_views`. Строка листа несёт и то, что
  лист обязан назвать рядом со способом (§4): `password_reset_at` — пароль подписи задан по
  ссылке нового пароля; у ключа — `key_reset_revoked_at` (ключ потом погашен новым паролем)
  и `key_bound_after_reset_at` (привязан после сброса пароля), из
  `platform/staff_passkey_history`. `memo_participants(staff, id)` — вид, автор, адресат,
  подписанты и статус без текста и без строки просмотра (кому пуш и какой: PR 4б-2, 5б). `list_memos(staff, MemoBox)` — «Ждут меня», «Мои», «Все»; строка (`MemoSummary`) несёт `signer_ids` — поиск кабинета по имени участника (PR 4б-3). Чужому —
  ERR-MEMO-001, как несуществующей. «Отправленная» — по `sent_at`: черновик, отозванный до
  отправки, видит только автор.
- `set_signature_sample(staff, SignatureStrokes)`, `current_signature_sample(staff)`,
  `revoke_signature_sample(user_id)` — образец подписи; «Сбросить подпись» гасит его вместе
  с ключами (`staff_team.reset_member_passkeys`), права проверяет кабинет.
- **Объяснительные** (§6). `create_demand(staff, employee_id, data, original_name) -> UUID` —
  требование с фото (обязательно; ERR-FILES-001…003): `manager` — у любого сотрудника отеля,
  `senior` — у `staff` и `senior` своих служб (иначе ERR-PLATFORM-002 `not_your_service`),
  `staff` и суперадмин — ERR-AUTH-003 (403); права — до песочницы.
  `begin_receipt(staff, id) -> ConfirmationRequest` → окно подтверждения →
  `confirm_receipt(staff, id, answer) -> datetime` — «Получил(а)»: только тому, у кого требуют
  (затребовавшему — ERR-MEMO-001), один раз (ERR-MEMO-002), с образцом подписи (ERR-MEMO-003);
  время подтверждения — время получения, возвращается и хранится срок (`explanation_due`).
  `get_demand(staff, id) -> DemandCard` — затребовавшему и сотруднику: состояние
  (`awaiting_receipt` / `open` / `expired` / `explained`), срок, способ и `proof_status`
  получения, объяснительные по нему (черновик — только автору) с отметкой `late`;
  `list_demands(staff, MemoBox)` — «Ждут меня» (сотруднику — не полученные), «Мои» (затребовал),
  «Все»; `read_demand_photo(staff, id) -> StoredFile` — фото участникам.
  `create_explanation(staff, ExplanationDraftInput)` — по требованию (`demand_id`: своё и уже
  полученное, иначе ERR-MEMO-001 / -002; адресат — затребовавший) или по своей инициативе
  (`addressee_id` — `senior` или `manager`); `update_explanation(staff, id, ExplanationText)` —
  свой черновик (адресата меняет только черновик без требования, иначе `addressee_fixed`);
  `create_supplement(staff, id, ExplanationText) -> UUID` — «Дополнить» поданную: новый черновик
  со ссылкой `based_on_id`, тем же адресатом и требованием. «Подать» — `SendIntent`: номер
  «ОБ-2026/3», сразу `submitted`; решения у объяснительной нет (`DecisionIntent` — ERR-MEMO-001),
  в «Ждут меня» адресата она не попадает. `MemoCard.demand_id` — требование объяснительной.
- `delete_expired_memos(drafts_before=...) -> int` — для прохода ретеншна: черновики, отправленные
  записи по сроку отеля; требование — тем же сроком от создания, но пока хранится объяснительная
  по нему, живёт и оно; фото требования удаляется до строки.

Коды — [errors.md](../../../../docs/runbooks/errors.md): ERR-MEMO-001 (404), -002 (409), -003 (409).

## Конфигурация

`TenantConfig.memos_enabled` (умолчание `false`), `memo_templates` (до 10, набор по
умолчанию — три шаблона §7), `memo_retention_days` (1095, от 365 до 3650), `holidays` (даты,
для срока объяснения: рабочие дни — пн–пт минус они; срок считается нажатием «Получил(а)»,
и праздник, вписанный позже, его не сдвигает) — `platform/memo_config.py`; онбординг переносит
все четыре.

## События

Не публикует и не потребляет. Пуши участникам — кабинет (`staff_portal/memos_push.py`),
канон `platform/staff_push`.

## Таблицы

Миграция 0071, RLS-канон 0002. FK на `users` без каскада — учётки гасятся, а не удаляются.

- `memos` — вид, номер (UNIQUE в отеле), шаблон, автор, адресат, шифртексты заголовка, текста,
  хэша содержимого и комментария решения, статус, «взамен», требование, времена (`sent_at` —
  отсчёт срока хранения, `submitted_at` — фиксация листа).
- `memo_signatures` — строка листа: сотрудник, роль, ответ, шифртексты замечания и
  доказательства, способ, образец, время; UNIQUE `(memo_id, user_id, role)`.
- `memo_signature_samples` — образец штрихами (шифртекст), действующий один на сотрудника.
- `memo_demands` — требование объяснения: кто, у кого, id фото (`uploaded_files`, назначение
  `memo_demand`), время получения с доказательством (шифртекст), срок; `memo_views` — просмотры;
  `memo_counters` — последний номер на `(tenant_id, kind, year)`, год — по поясу отеля.

Шифруется всё, что несёт текст, и хэш с доказательством: по копии базы не прочесть текст и не
проверить догадку о нём (ревью PR #938). ПД — строки [PII_REGISTRY](../../../../docs/PII_REGISTRY.md).

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime, session_scope, AppError, логирование,
tenancy), `hospitality.platform` — `config` (конфиг отеля), `field_encryption` (шифртексты),
`files` (фото требования),
`staff_confirm` (подтверждение и проверка доказательства), `staff_passkey_history` (судьба ключа
для листа), `staff_team` (действующие сотрудники). Внешних нет.

## Метрики и логи

Метрик нет. Логи: `memo_draft_created`, `memo_draft_updated`, `memo_withdrawn`,
`memo_submitted`, `memo_action_confirmed` (action, method, status), `memo_explanation_draft_created`
(by_demand, supplement), `memo_demand_created`, `memo_demand_received` (method),
`memo_demand_photo_cleanup_failed` (WARNING), `memo_signature_sample_set`,
`memo_signature_sample_revoked`, `memo_content_changed_after_send` (WARNING),
`memo_retention_config_unavailable` (WARNING) — id, вид, статус и способ; без текстов,
заголовков и имён (§8).

## Типовые сценарии изменения

- Новое действие под подтверждением → `Intent` в `schemas.py`, ветка в `signing._prepare`
  и `reading._signed_subject` — подтверждаемое должно собираться одинаково в обоих местах.
- Новое поле в неизменном содержимом → `codec.content_hash`; прежние доказательства с ним не
  сойдутся — только с миграцией данных или версией хэша.
