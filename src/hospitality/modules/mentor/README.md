# mentor — «Наставник» персонала: кодекс отеля, диалоги и оценки

## Назначение

Сотрудник спрашивает о стандартах отеля и получает ответ по статьям кодекса со ссылкой, а в
«Тренировке» отвечает на вопросы по статьям ([spec 0048](../../../../docs/specs/0048-mentor.md),
issue #492). Отдельный модуль, а не база знаний гостя — [ADR-023](../../../../docs/adr/ADR-023-mentor-module.md):
стандарты персонала не должны попасть в ответ гостю (§7.6).

Модуль о модели не знает: поиск принимает готовый вектор, запись диалога — готовый текст.
Эмбеддинги, промпты и поток ответа — композиционный слой `ai/mentor/` (ADR-023), права и экраны
— кабинет (PR 4 серии), флаг раздела — `TenantConfig.mentor_enabled` (проверяет кабинет).

## Состав

| Файл | Что даёт |
| --- | --- |
| `codex.py` | Разбор разметки «часть → раздел → глава → статья» (перенос `hotelbot/kodeks.py`); `CodexMarkupError` с файлом и строкой; предел статьи `MAX_ARTICLE_CHARS` |
| `models.py` | Пять тенантных таблиц (RLS-канон, миграция 0043), `ConversationMode`, `MessageRole`, `QuizGrade` |
| `service.py` | Кодекс и индекс статей: замена целиком, поиск по косинусу, досчёт векторов, выбор статьи «Тренировки»; ERR-MENTOR-001/002 |
| `dialogs.py` | Диалоги, вопрос и ответ «Тренировки», оценки, «Начать заново», ретеншн; ERR-MENTOR-003 |
| `schemas.py` | `CodexFile`, `CodexArticleDraft/Read` (`full_text` — статья, как её видят поиск и модель), `CodexInfo`, `ConversationRead` (`history` — не пересказанное в резюме), `NewMentorMessage` |
| `api.py` | Публичный интерфейс (единственная точка входа извне) |
| `tests/` | Разбор (образцы «Наставника»), замена с ошибкой, поиск, досчёт векторов, выбор статьи, диалоги и чужой сотрудник, оценки, ретеншн, RLS |

## Публичный API

- `parse_codex_files(files) -> list[CodexArticleDraft]` — статьи по порядку; ошибка разметки —
  `ERR-MENTOR-001` (422, «Файл 20_front_office.md, строка 41: статья 15 вместо 14»). Файлы
  читаются по имени.
- `replace_codex(files, *, embeddings, embedding_model, uploaded_by_user_id) -> CodexInfo` —
  кодекс и статьи целиком одной транзакцией; ошибка разметки — до записи, старый кодекс
  остаётся. `embeddings=None` — без векторов (сид). Две загрузки разом упорядочивает upsert строки
  кодекса.
- `get_codex()`, `has_codex()`, `load_codex_document()` — сведения, «загружен ли», дерево для
  страницы «Кодекс».
- `search_articles(vector, *, limit)` — ближайшие по `<=>` без индекса; статьи без вектора не
  участвуют. `get_articles(numbers)` — по номерам, несуществующие отброшены.
- `list_articles_needing_embedding(model)` / `store_article_embeddings(vectors, *, model)` —
  статьи без вектора или с вектором другой модели и запись векторов.
- `pick_quiz_article(*, chapter_number, near, exclude)` — статья от `QUIZ_MIN_ARTICLE_CHARS`
  (250) знаков; длинных нет — любая. С вектором темы — ближайшая из трёх не спрошенных недавно,
  без темы — случайная.
- `get_conversation(user_id, mode)`, `append_messages(...)`, `compact_history(...)` — диалог
  сотрудника (один на `(user_id, mode)`), реплики по порядку, резюме старой части.
- `start_quiz_question(...)`, `active_quiz_question(user_id)`, `finish_quiz_answer(...)` — вопрос
  «Тренировки» (статья уходит в последние `QUIZ_NO_REPEAT` = 6), ответ и проверка, строка оценки;
  вопроса нет или он сменился — `ERR-MENTOR-003` (409).
- `delete_conversation(user_id, mode)` — «Начать заново».
- `delete_expired_mentor_texts(*, created_before)` — реплики и резюме старше границы (зовёт
  `channels/common/retention.py`, срок — как у текстов гостя, spec 0032). Идемпотентна.

## События

Не публикует и не потребляет.

## Таблицы

Миграция 0043 (RLS-канон 0002, `CREATE EXTENSION vector`): `mentor_codexes` (файлы `{name, text}`
массивом, одна строка на тенанта), `mentor_codex_articles` (статья + `vector(1536)` и модель
вектора), `mentor_conversations` (резюме и момент резюме, текущий вопрос, последние статьи
вопросов), `mentor_messages` (реплика, ссылки на статьи, `in_summary`), `mentor_quiz_answers`
(оценка без текста ответа). Строки ПД — `docs/PII_REGISTRY.md`.

## Зависимости

Внутренние: `hospitality.shared` (Base, UTCDateTime, session_scope, AppError, логирование).
Внешние: `pgvector` — тип колонки и оператор `<=>`.

## Типовые сценарии изменения

- Смена модели эмбеддингов с другой размерностью → миграция колонки и
  `ARTICLE_EMBEDDING_DIMENSIONS` (тест сверяет его со шлюзом); векторы досчитаются сами.
- Статистика «Где ошибаются» и «Обнулить» (PR 4) → чтения `mentor_quiz_answers` в `dialogs.py`.
- Правило разметки → `codex.py` и образцы в `tests/test_codex_parser.py`; старые загруженные
  кодексы перечитываются этим же разбором.
