# channels/common — общее ядро гостевых каналов (spec 0027 §2)

## Назначение

Канал-агностичная часть гостевых каналов: единая персистенция диалога,
общий «ход гостя» и событие эскалации. Транспортные адаптеры
(`channels/telegram`, `channels/web`, будущий WhatsApp) зовут это ядро —
инварианты spec 0022/0023/0025 живут в одном месте, а не в копиях. Вынесено
из `channels/telegram` с приходом второго канала (PR #113, поведение не
менялось). Композиционный слой (§5.1); ядро НЕ импортирует транспорты.

## Состав

| Файл | Что даёт |
| --- | --- |
| `models.py` | `Conversation`, `Message`, `RequestOrigin`, `ConversationEscalation`, `UnansweredQuestion` — тенантные таблицы диалога (§9, RLS-канон; миграции 0008/0009/0015/0026/0029/0039) |
| `store.py` | Идемпотентная запись диалога (P-8), гейт P-9, привязки заявок, окно истории `MAX_HISTORY_MESSAGES` (#74), выборка для страницы веб-чата, обратный поиск заявки по реплаю на сообщение бота (белый список ключей `staff:request_created`/`staff:request_unclaimed`/`staff:request_stalled`/`staff:note_prompt`/`staff:cancel_prompt`; `load_staff_prompt` — вид вопроса бота с ForceReply и заявка по его ключу, spec 0057 §2; **в пределах диалога реплая**: `external_message_id` уникален только внутри чата, а чатов служб шесть — spec 0026, issue #206) |
| `guest_turn.py` | `run_guest_turn` — ход гостя: ЧП-перехват 0034 первым действием (без LLM и раньше лимита; его реплика уходит и при упавшей эскалации, #382) → rate-limit и предел длины 0023 ДО оркестратора (`chat_limits.py`) → история/pending/снапшот 0025 → оркестратор с потолком хода `GUEST_TURN_DEADLINE_SECONDS` (spec 0055: дольше гость модель не ждёт, дальше — деградация §7.8) → pending → привязка ADR-011 → эскалация 0022 (через `escalate`, в outbox ДО реплики; не дошла до человека — реплика-обещание заменяется текстом «позвоните на ресепшен», spec 0051) → реплика → строка «вопрос без ответа» 0036 §6 (последней: её сбой не отнимает у гостя реплику); транспорт — параметр `reply`, ключ лимита — параметр `rate_limit_key` (telegram — chat_id, web — stay_id), `verified_room_number` — комната из привязки (web), `verified_stay_id` — её Stay, уходит в заявки хода (spec 0054); возвращает id заявки, созданной ходом (иначе None) — web по нему подсказывает гостю, где ждать итог (#223) |
| `chat_limits.py` | Лимиты гостевого чата (spec 0023): `refuse_if_rate_limited` — ступени до оркестратора, от жёсткой к мягкой: денежная `guest_chat_spend` (peek; потолок — `GUEST_CHAT_DAILY_BUDGET_SHARE` × `LLM_TENANT_DAILY_BUDGET_USD` за UTC-сутки, §5, issue #404), `guest_chat_daily`, `guest_chat_window`; `refuse_if_too_long` — предел `MAX_GUEST_MESSAGE_CHARS` (1000); `record_chat_spend` — цена хода (`OrchestratorTurn.llm_cost_usd`) в счёт чата после хода. Тексты отказов и лог-код `ERR-CHANNEL-003` |
| `escalation_request.py` | `escalate` — единственный вход хода гостя в эскалацию (spec 0039 §2): у отеля с `TenantConfig.escalation_category_key` сначала заявка в этой службе (`origin=escalation`, шаблоны карточек §2.2 — здесь, ЧП срочная; «Гость просит сотрудника» — spec 0040), затем `publish_escalation` со ссылкой на неё; Stay веб-гостя (`verified_stay_id`) карточка пишет в заявку (spec 0054); возвращает, есть ли у эскалации адресат-человек — карточка в кабинете (новая или невзятая прежняя) или непустой `TELEGRAM_STAFF_CHAT_ID` (spec 0051 §2). Несрочная не заводит вторую карточку, пока у диалога есть невзятая (`new`) эскалационная (§2.5); шаг заявки — best-effort, никакое исключение не мешает эскалации и ответу гостю (§2.6). Привязки к диалогу у такой заявки нет (§2.7) |
| `consent.py` | **CANONICAL** согласие гостя (spec 0029): `CONSENT_VERSION`, тексты kk/ru/en (дословная копия `docs/legal/consent-text.md`, дрейф ловит `tests/test_legal.py`), выбор языка и правило `is_consent_current`. Где ХРАНИТСЯ факт — забота канала: telegram — `conversations.consent_at/_version`, web — `guest_sessions` (§1 спеки) |
| `unanswered_questions.py` | Вопрос гостя, не покрытый справочником отеля (spec 0036 §6): `record_unanswered_question` — одна строка `unanswered_questions` по полю исхода `OrchestratorTurn.unanswered_question`; `list_unanswered_questions(created_after=, limit=)` — новые сверху для раздела «Гости спрашивали, а ответа не было» страницы «Справочник отеля» (окно и предел задаёт страница — канон `count_escalations`). Канон — сосед `events.py` (факт целиком в одном файле), но события здесь нет: человека звать не нужно, эскалацией это не является. В `store.py` не уехало по R-3 — тот файл уже на границе ~400 строк |
| `events.py` | Факт эскалации целиком: событие `ConversationEscalated`, `publish_escalation` (пишет строку `conversation_escalations` — со ссылкой `request_id` на заведённую заявку, если она есть, — в ТОЙ ЖЕ транзакции, что публикует событие — spec 0035 §6.1), `list_escalation_request_ids(conversation_id)` — заявки, заведённые эскалациями диалога (опора дедупликации spec 0039 §2.5), и `count_escalations(created_after=, created_before=)` — число «бот звал сотрудника» за окно, которое задаёт вызывающая сторона (канон `platform/events.py`) |
| `retention.py` | Ретеншн гостевых текстов (issue #42, spec 0032): из цикла воркера раз в `worker_retention_interval_seconds` удаляет `messages` старше `messages_retention_days` (90 — обещание политики конфиденциальности), `unanswered_questions` того же возраста (гостевой текст, spec 0036 §6 — по СОБСТВЕННОМУ возрасту строки: у живого диалога каскада не будет никогда), опустевшие давно не обновлявшиеся `conversations` (каскадом — `request_origins`; согласие умирает с диалогом) обезличивает свободный текст заявок через `modules/requests.api` и удаляет строки журнала изменений кабинета того же возраста через `platform.staff_actions.delete_staff_actions_before` (spec 0041 §2.5 — своего срока у журнала нет; счётчик `staff_actions_deleted` в `guest_text_retention_completed`), обнуляет замечания хаускипинга о номере через `modules/housekeeping.api` (spec 0046 §8; счётчик `turnover_notes_cleared`), удаляет файлы гостя того же возраста (spec 0047 §6), обезличивает запросы реквизитов для АВР и удаляет брошенные — без заявки старше суток — через `modules/documents.api` (spec 0049 §3; счётчики `act_requests_anonymized`, `act_requests_abandoned_deleted`), удаляет реплики «Наставника» и обнуляет их резюме через `modules/mentor.api` (spec 0048 §4; счётчики `mentor_messages_deleted`, `mentor_summaries_cleared`), обезличивает заказы в номер (обнуляет связь с проживанием и гостем, состав и суммы остаются) и удаляет брошенные — без заявки старше суток — через `modules/room_service.api` (spec 0038; счётчики `room_service_orders_detached`, `room_service_orders_abandoned_deleted`). Обход ВСЕХ тенантов (`list_tenant_ids`) под `tenant_context` каждого (P-4); сбой одного тенанта — `ERR-CHANNEL-004`, остальные обходятся |

Текст в `messages.text` приходит уже с замаскированными платёжными картами —
это контракт нормализации (`channels/base.py`, валидатор `NormalizedMessage`
поверх `shared/pii.py`, spec 0031, NG-3); у `kind=CALLBACK` текст — машинные
callback-данные и не маскируется. Ядро и хранилище сырой PAN не видят: `store.py`
берёт `message.text`, а не `message.raw_text` (оригинал для разбора команд
персонала, spec 0031 §2).

Статические тексты (UNSUPPORTED/DEGRADED и «звать некому» `no_staff_reply` —
`guest_turn.py`, отказы лимитов и лог-код `ERR-CHANNEL-003` — `chat_limits.py`)
трёхъязычны (kk, ru, en с новой строки, порядок канона согласия; #224) и
канал-нейтральны; `no_staff_reply` берёт телефон и цифру ресепшена
из конфига тенанта, как текст ЧП.
`refuse_if_rate_limited` публична: consent-gate обязан считать те же ступени,
что и ход гостя (одно место правила, P-12). Входящие длиннее предела
сохраняются в `messages`, но в историю модели не попадают (`load_dialog_history`).
Отказ лимита уходит репликой один раз на окно; канал, передавший в ход `pause`,
на остальных сообщениях окна получает текст той же ступени статусом, а не
репликой — web так не молчит (#222); Telegram `pause` не передаёт.

**Страховка хода (issue #43).** Ключ идемпотентности входящего отмечает
«получено», а не «обработано», поэтому `run_guest_turn` сам не отдаёт каналу
исключение, если гость ещё не получил ни одной реплики: ход, упавший любой
ошибкой до первой попытки `reply`, кончается как деградация §7.8 — эскалация
`llm_unavailable` и `DEGRADED_REPLY` (адресата нет или эскалация упала —
`no_staff_reply`), ERROR `ERR-CHANNEL-006`. Упал после попытки — исключение
летит дальше, как раньше: вторая реплика «не могу ответить» соврала бы.
Телефон ресепшена для `no_staff_reply` и текста ЧП читается с перехватом любого
сбоя: без конфига текст действует и так.

## События

Публикует: `conversation.escalated` (spec 0022); у отеля со службой эскалаций
та же эскалация заводит заявку через `modules/requests` — `request.created`
публикует уже модуль заявок (spec 0039 §2). Потребляет: ничего
(подписчики уведомлений — `channels/telegram/notifications.py`, staff живёт в
Telegram; гостевые уведомления там канал-осознанны: telegram — push, web —
запись исходящего под poll).

## Таблицы

`conversations` / `messages` / `request_origins` — описание и колонки в
`channels/telegram/README.md` (исторически) и в докстрингах `models.py`;
`conversations.guest_identity_id` (0015) — аддитивная связь с
`modules/guests`, без FK (граница модулей);
`conversations.consent_at`/`consent_version` (0016) — доказательная запись
согласия telegram-гостя (spec 0029, строка в `docs/PII_REGISTRY.md`).

`conversation_escalations` (0026, spec 0035 §6.1) — durable-счёт эскалаций:
`tenant_id`, `conversation_id` (FK, **ON DELETE SET NULL**, NULLABLE), `reason`
(VARCHAR(32) — значение `EscalationReason` строкой), `created_at` (индекс —
по нему фильтрует счёт за сутки; дедупликация spec 0039 ищет по индексу
`conversation_id`). Строка пишется в единственном месте
публикации события (`publish_escalation`) и в той же транзакции. `SET NULL`, а
не `CASCADE`: ретеншн гостевых текстов через 90 дней сносит диалог, а факт
«бот позвал человека» текста гостя не содержит и переживать ретеншн вправе —
иначе число «бот звал сотрудника» молча обнулялось бы задним числом каждые три
месяца. Персональных данных таблица не содержит (spec 0035 §12), в ретеншн
гостевых текстов не входит.

`unanswered_questions` (0039, spec 0036 §6) — вопросы гостей, на которые в
справочнике отеля не нашлось факта; источник его пополнения и предмет страницы
«Справочник отеля» (окно 7 дней). Колонки: `tenant_id`, `conversation_id` (FK,
**ON DELETE CASCADE**, NOT NULL), `question` (VARCHAR(200) — предел один на схему
инструмента, разбор сигнала и колонку), `created_at` (индекс — оба чтения
фильтруют по возрасту). Ни статуса, ни «скрыть»: окно страницы стирает список
само. `CASCADE`, а не `SET NULL` соседней таблицы, — потому что содержимое
другое: `question` есть пересказ вопроса гостя, то есть гостевой текст (строка в
`docs/PII_REGISTRY.md`), и переживать ретеншн он не вправе. Сам ретеншн сносит
строку по её собственному возрасту (90 дней, spec 0032), каскад — второй рубеж.

`conversation_escalations.request_id` (0029, spec 0039 §2.5) — UUID NULL **без
FK**: заявка, которую эскалация завела (непрозрачная ссылка на таблицу чужого
модуля, довод `request_origins.request_id`). NULL — заявки нет: служба
эскалаций не задана, создание не удалось, эскалацию поглотила дедупликация или
строка старше миграции. Значит, «эскалаций за день» (`count_escalations`) может
быть больше, чем заявок с `origin=escalation`, — и это честно. Индекса нет:
поиск идёт по `conversation_id`.

## Наблюдаемость (spec 0039 §8)

Журнал: `escalation_request_created` (`request_id`, `reason`, `is_urgent`),
`escalation_request_deduplicated` (`request_id` невзятой карточки),
`escalation_request_skipped` (`cause`: `no_category_key` — служба не задана,
норма; `category_missing` + `ERR-REQUESTS-001`; код `AppError`; `unexpected` —
только `error_type`, без текста исключения: в тексте `IntegrityError`
параметры SQL, то есть реплика гостя). Отказы — уровнем WARNING, не ERROR:
ERROR-лог уходит событием в Sentry вместе с телом запроса гостя (#433). Метрика
`escalation_requests_total{outcome}`: `created` | `deduplicated` | `disabled` |
`failed`. `escalation_without_staff` (`reason`, INFO) — эскалации некому принять,
гость получил «позвоните на ресепшен» (spec 0051). Текста гостя в журнале нет.

## Зависимости

Kernel (`shared`, `platform.config` — телефон ресепшена и служба эскалаций),
домен `modules/requests` (api), композиционный `ai`
(оркестратор/gateway/tools) и `channels/base`. Транспорты (`telegram`, `web`)
импортируют ядро; обратное запрещено (контракт import-linter «common не
импортирует транспорты»).

## Типовые сценарии изменения

- **Новый гостевой канал** — транспортный пакет `channels/<name>`, зовущий
  `ensure_conversation`/`insert_inbound_message`/`run_guest_turn` со своим
  `reply`; ядро не меняется (карта канонов CLAUDE.md).
- **Новый инвариант хода** (лимит, политика, квота) — здесь, один раз; каналы
  получают его бесплатно.
- **Правка текстов отказов** — `guest_turn.py` (kk, ru, en, без LLM; три языка
  держит `tests/test_guest_static_texts.py`).
- **Новая причина эскалации** (`EscalationReason`, например «позовите
  сотрудника» #101) — строка в `_CARD_TEMPLATES` (`escalation_request.py`):
  без неё падает тест полноты, механизм не меняется (spec 0039 §6).
- **Правка текста и маркеров ЧП** — `ai/urgency.py` (spec 0034): здесь
  только вызов перехвата и чтение телефона ресепшена и цифры «из номера»
  (`reception_room_dial`) из конфига тенанта.
- **Правка текста согласия** — сначала `docs/legal/consent-text.md` (источник
  истины), затем копия в `consent.py` и `CONSENT_VERSION` — в одном PR: гости с
  прежней версией пройдут гейт заново (spec 0029 §6).
