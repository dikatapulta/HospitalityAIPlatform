# feedback — гость оценивает выполненную заявку

## Назначение

Гость ставит выполненной заявке палец вверх или вниз и, при пальце вниз, может дописать
«что не так» ([spec 0076](../../../../docs/specs/0076-guest-request-feedback.md), #899).
Палец вниз сразу заводит карточку в службе эскалаций отеля, пока гость в отеле;
комментарий дописывается в неё. Сводка дня считает оценки по службам. Модели на пути нет.

Серия из трёх PR (§9): домен (этот модуль, миграция 0068) — веб-чат (маршруты и виджет,
`channels/web`) — кабинет (плитка «Оценки гостей» в сводке дня, `staff_portal/summary.py`). Кнопки у
гостя — с PR 2 (#927), у отеля с включённым выключателем.

## Состав

| Файл | Что даёт |
| --- | --- |
| `api.py` | Публичный интерфейс: единственная точка импорта извне (R-5) |
| `models.py` | `RequestFeedback` — тенантная таблица `request_feedback` (канон RLS); `FeedbackRating` — `up` / `down` |
| `schemas.py` | `FeedbackRead` (итог вызова для ответа гостю), `FeedbackState` (виджет), `FeedbackCount` (сводка); `COMMENT_MAX_CHARS` = 500 |
| `service.py` | `rate_request`, `add_comment`, `feedback_states`, `count_feedback`, `clear_expired_comments`; коды ошибок |
| `cards.py` | Карточка пальца вниз и второй карточки — сборка текстов §8 и запись через `requests_api` |
| `tests/` | Коды §4.5, двойное касание с принудительным чередованием (одна строка и одна карточка), карточка и её сбой, комментарий в карточку, вторая карточка и вторая попытка доставки, маскирование карты, состояние виджета, счёт по службам, срок комментария, RLS, валидатор выключателя |

## Публичный API

- `rate_request(request_id, *, stay_id, rating) -> FeedbackRead` — оценить заявку `done`,
  у которой `stay_id` — Stay сессии гостя (вызывающий берёт его только из сессии). Повтор той
  же оценки — прежняя без новой карточки (P-8), другая — ERR-FEEDBACK-003. У `down` —
  карточка: суть «Гость недоволен заявкой #N «{суть}» — {служба}» (суть обрезается с «…»),
  в подробностях «Закрыл: {имя}», служба — `escalation_category_key`, комната и Stay —
  оценённой заявки, `origin=feedback`, не срочная. Вставка оценки, карточка и
  `card_request_id` — одна транзакция с `INSERT … ON CONFLICT DO NOTHING RETURNING`: второе
  касание ждёт первое на уникальном индексе и карточку не заводит.
- `add_comment(request_id, *, stay_id, comment) -> FeedbackRead` — один комментарий к `down`
  (атомарный `UPDATE … WHERE rating='down' AND comment IS NULL AND comment_at IS NULL`),
  иначе ERR-FEEDBACK-004. Текст обрезается по краям, пустой или длиннее 500 — `ValueError`
  (маршрут отвечает 422 раньше). Номер карты маскируется до записи (NG-3). Карточка
  невзятая — строка «Гость написал: «…»» дописывается в неё; взятая, закрытая или полная —
  вторая карточка «Гость дописал к оценке заявки #N: «…»» (канон spec 0039 §2.5); карточки
  нет (сбой при оценке) — комментарий заводит её сам, с комментарием в подробностях.
- `feedback_states(request_ids, *, stay_id) -> dict[UUID, FeedbackState]` — для виджета:
  только заявки `done` этого Stay; оценки выключены — пусто. `passed_to_staff` — карточка
  по оценке есть: после перезагрузки страница не обещает гостю «передадим», если её нет.
- `count_feedback(*, start, end, category_ids=None) -> list[FeedbackCount]` — `up` и `down`
  по службам за `[start, end)` по времени оценки; `category_ids` — срез старшего.
- `clear_expired_comments(cutoff) -> int` — для джобы ретеншна: `comment` → NULL у
  комментариев старше `cutoff` по `comment_at`; оценка, служба и время остаются.

`FeedbackRead.passed_to_staff` — дошло ли это действие до сотрудников: у `down` — карточка
есть, у комментария — он записан в карточку. False — ответ гостю без «передадим» и
«передали» («Спасибо за оценку!»). Коды: ERR-FEEDBACK-001 выключено (404), -002 заявку нельзя
оценить (404), -003 оценена иначе (409), -004 комментарий неприменим (409), -005 — сбой
карточки (только лог) — [errors.md](../../../../docs/runbooks/errors.md).

## Конфигурация

`TenantConfig.guest_feedback_enabled` (умолчание `false`): выключено — маршруты 404, виджета
нет. Включить без `escalation_category_key` нельзя — валидатор конфига. Включают руками;
онбординг переносит прежнее значение (`build_config`).

## События

Не публикует и не потребляет. Карточка — заявка, её события публикует `requests`.

## Таблицы

Миграция 0068, RLS-канон 0002; FK в чужие модули нет.

- `request_feedback` — `request_id` (UNIQUE `(tenant_id, request_id)`), `category_id` (служба
  заявки на момент оценки), `rating` (VARCHAR(8)), `comment` (Text NULL), `comment_at`,
  `card_request_id` (NULL — карточки нет), `created_at` (индекс — окно сводки), `updated_at`.

Той же миграцией — `messages.request_id` (канал, `channels/common`): итог заявки, под
которым веб-чат ставит кнопки. ПД — строка [PII_REGISTRY](../../../../docs/PII_REGISTRY.md):
комментарий живёт срок текстов гостя (шаг 13 ретеншна).

## Зависимости

Внутренние: `hospitality.shared` (Base/UTCDateTime/utc_now, session_scope, AppError,
логирование, tenancy, маскирование карт), `hospitality.platform.config`
(`load_tenant_config`), `modules/requests` — через его `api.py`. Внешних нет.

## Метрики и логи

Метрик нет. Логи: `feedback_rated` (request_id, rating, category_id), `feedback_repeated`,
`feedback_commented` (outcome: `appended` / `new_card` / `failed`), `feedback_card_created`
(request_id, card_request_id), `feedback_card_failed` (ERROR, `error_code` ERR-FEEDBACK-005,
`cause`, `error_type`) — без текста гостя и текста исключения.

## Типовые сценарии изменения

- Тексты карточки → константы `cards.py` (§8 спеки; показ основателю до правки).
- Оценка в Telegram → маршрут канала поверх того же `rate_request` (запись в
  [later/](../../../../docs/later/2026-11-10-pr914-feedback-telegram.md)).
- Звёзды вместо пальцев → новый `FeedbackRating`, VARCHAR(8) хватит; счёт сводки — по-новому.
