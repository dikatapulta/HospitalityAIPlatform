# channels/showroom — страница 3D-витрины для посетителей без проживания

## Назначение

Анонимная поверхность ([ADR-025](../../../../docs/adr/ADR-025-public-showroom-without-login.md),
[spec 0069](../../../../docs/specs/0069-sales-showroom-3d.md) §2.2): посетитель на домене платформы
смотрит здание, этажи, стороны и номера и подбирает их условиями. Контекст тенанта канал ставит
сам по slug (как веб-чат, ADR-008 §6); гостевых сессий и данных гостей здесь нет.

## Маршруты (`/showroom/…`)

| Маршрут | Что |
| --- | --- |
| `GET /showroom/static/{имя}` | Статика: `viewer.js`, `showroom.js`, `assistant.js`, `request_form.js`, `showroom.css`, `request_form.css`, `access.js`, three.js; верная `?v=` — кэш навсегда |
| `GET /showroom/{slug}` | Страница; без доступа — экран «Введите код доступа»; язык — `?lang=`, Accept-Language, иначе en |
| `POST /showroom/{slug}/access` `{code}` | Код → cookie `showroom_access` (204); неверный — 403 `005`; лимит — 429 `003` |
| `GET /showroom/{slug}/scene` | Сцена для просмотрщика |
| `GET /showroom/{slug}/rooms?floor_from&floor_to&side&view_key&type_key&count` | Подбор `find_rooms` |
| `POST /showroom/{slug}/navigate` `{text ≤ 300, language, audience}` | Помощник (§2.3): `{commands, reply, source}` — `rules`, `model` или `fallback`; лимит — 429 `003` |
| `POST /showroom/{slug}/requests` | Запрос группы или гостя (PR (в)): после 404 и 403 — ловушка `website` (200 без записи), тело вне правил (422 `004`, пути полей без значений), лимиты `showroom_request_ip` 5 в час на адрес и `showroom_request_tenant` 50 в сутки отеля (429 `003`, fail-open), `showroom_api.create_request` — 201 новый, 200 повтор ключа; посетителю — только `{"status": "sent"}` |

Порядок отказов: неизвестный slug, витрина выключена, сцены нет, режим только кабинетный — 404
`ERR-SHOWROOM-001` раньше всего; затем доступ — публичный режим пускает всех, по коду — только с
cookie, подписанной текущим кодом (403 `005`, без причины).

Cookie — `HttpOnly; Secure; SameSite=Strict; Path=/showroom/{slug}`, 7 дней; значение — подпись
модуля (`showroom.api.access_token`). Ввод кода — две ступени лимита канона spec 0027 §3.3:
`showroom_code_verify_ip` 20 за 10 минут на адрес (`shared/clientip.py`), затем
`showroom_code_verify_tenant` 200 за 10 минут на отель; Redis недоступен — fail-open (канон 0023).
Фразы помощнику — `showroom_navigate_ip` 30 за 10 минут на адрес и `showroom_navigate_tenant`
600 в сутки на отель, у посетителя и в кабинете вместе (тот же fail-open).

## Файлы

| Файл | Что |
| --- | --- |
| `router.py` | Маршруты выше; PR (б) и (в) добавляют по строке `include_router` (`navigate.py`, `request_form.py`) |
| `navigate.py` | Помощник: маршрут посетителя и `answer` — лимиты, сцена, «сегодня» в поясе отеля, ход `ai/showroom` (его же зовёт кабинет, `staff_portal/showroom_api.py`) |
| `access.py` | Витрина по slug (`open_showroom`), доступ, проверка кода с лимитами, cookie |
| `page.py` | HTML страницы и экрана кода, заголовки (CSP `default-src 'self'`, `noindex`), `viewer_config` — общий с кабинетом |
| `texts.py` | Надписи en/ru/kk (ru — spec §5, en и kk — перевод PR а, founder-review); `assistant_*` — поле помощника (PR б) |
| `assets.py` | Реестр статики с версией содержимого |
| `assets/viewer.js` | Просмотрщик: здание из чисел сцены на three.js; API `focusFloor`, `focusRoom`, `setView`, `highlight`, `select`, `reset`, событие `select` |
| `assets/showroom.js` | Разметка страницы и кабинета: вкладки «Для групп · Для гостей», виды, поиск номера, этажи, панель подбора, карточка номера; `window.Showroom` и события `showroom:ready`, `showroom:audience`, `showroom:select`, слоты `[data-sr-slot=assistant|request]` для PR (б) и (в) |
| `request_form.py` | Маршрут запросов (выше) и `page_block` — блок форм для страницы: «сегодня» по поясу отеля, пределы, надписи трёх языков JSON'ом, файлы статики |
| `request_texts.py` | Надписи форм en/ru/kk (ru — дословно spec §5, en и kk — перевод окна (в), founder-review) |
| `consent.py` | Строка согласия под формами со ссылкой на `/legal/showroom-privacy` и `SHOWROOM_CONSENT_VERSION`; дословность с `docs/legal/showroom-privacy.md` сверяет `tests/test_legal.py` |
| `assets/request_form.js` | Формы «Для групп» и «Для гостей» и карточка «Проверьте запрос»: встаёт в слот `request` по `showroom:ready`, вкладка — `showroom:audience`, номер с модели — `showroom:select`, виды и типы — `window.Showroom.scene`, черновик помощника — `showroom:draft` из `assistant.js`. Отправляет только кнопка посетителя (P-9) |
| `assets/request_form.css` | Раскладка форм; кнопки и поля — классы страницы (`.sr-btn`, `.sr-input`) |
| `assets/assistant.js` | Блок помощника в слоте `assistant`: фраза → `cfg.navigateUrl`; команды — методами просмотрщика и `Showroom.applyRooms`, «выше / ниже» — от этажа на экране, `draft_request` — событием `showroom:draft` для формы запроса (PR в); реплика — только `textContent`; нет ответа — «Помощник отдыхает…» |
| `assets/three-0.170.0.module.min.js` | three.js r170, официальная сборка из npm без правок (MIT, `three-LICENSE.txt`; sha256 — в `assets.py`) |

## Ключевые решения

- Скрипты и стили — свои файлы, а не встроенные в HTML (spec §2.2 говорит «инлайн»): CSP
  `default-src 'self'` встроенных не пускает, ослаблять её ради страницы без входа — хуже.
- Своя орбита камеры вместо `OrbitControls`: тот импортирует `three` голым именем, а карта импорта —
  встроенный скрипт.
- Разметку рисует `showroom.js` только через `textContent`: названия в сцене пишет отель.
- Подбор панели идёт на сервер (`find_rooms`), а не фильтром в браузере: один подбор у панели и
  помощника (P-5).

## Наблюдаемость

Логи: `showroom_access_granted`, `showroom_access_rejected`, `showroom_rate_limited`,
`showroom_navigate_rate_limited` (без адреса и без кода); ход помощника — `showroom_navigated`,
`showroom_command_dropped`, `showroom_model_unavailable` (`ai/showroom`, без фразы посетителя).
Запросы (PR (в)): `showroom_request_trap`, `showroom_request_invalid` (имена полей),
`showroom_request_rate_limited` (`scope`, `count`, `limit`; адреса нет) — ничего из полей формы.
Метрики: `showroom_navigations_total{source}`, запросов — `showroom_requests_total{kind}` (модуль).
