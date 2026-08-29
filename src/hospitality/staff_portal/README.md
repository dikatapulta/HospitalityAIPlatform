# staff_portal — кабинет персонала (composition-слой)

## Назначение

Server-rendered веб-кабинет персонала отеля (spec 0033, ADR-014): очередь
заявок, заселение с QR, управление сотрудниками. Условие запуска пилота
(issue #48). Слой композиции (FOUNDATION §5.1 «Web (персонал/админ)»):
импортирует `platform/` и `api.py` доменных модулей; сам не импортируется
никем, кроме composition root (контракт 7 import-linter). PR C дал каркас
(вход/выход, выбор отеля, layout и CSS-канон), PR D — очередь заявок
(закрывает #56), PR E — страницу заселения (Stay, QR bind-ссылки, код 6 цифр),
PR F — страницу «Сотрудники» и приглашения (закрывает #48). Форму ручного
приёма заявки добавила серия spec 0035 (#299): без неё заявку, о которой гость
сказал по телефону, вносить было некуда, и у Exit-критерия Phase 1 «≥ 70%
заявок без участия ресепшена» не было знаменателя.

## Состав

| Файл | Что даёт | Задача |
| --- | --- | --- |
| `router.py` | Страницы: логин/логаут, выбор отеля, главная `/staff/{tenant_slug}`, очередь `…/requests` (+fragment для поллинга), форма ручного приёма `…/requests/new`, заселение `…/checkin` (GET-поиск + POST-форма), сводка дня `…/summary`, сотрудники `…/team`, отдача статики; канон `_page_context` (авторизация страницы с браузерными исходами) и CSRF-контракт (докстринг модуля); подключает `invites` и `api_router` | #48 PR C/D/E/F, #299, #300 |
| `api_router.py` | JSON-действия: очередь (взять/готово/отменить + создать вручную, `origin=staff_manual`), карточка Stay (bind-link/reissue-code/move/extend/checkout/bindings) и состав команды (invites/members) — `require_role` напрямую, CSRF-щит `Content-Type: application/json` + непустой same-origin `Origin` (403 `ERR-AUTH-009`) | #48 PR D/E/F, #299 |
| `browser.py` | Браузерная обвязка обоих роутеров страниц: `PAGE_HEADERS` + `html_page()`, CSRF-щит HTML-форм (`is_cross_origin`), cookie сессии, тексты отказов аутентификации (`AUTH_ERROR_MESSAGES`). Выделено из `router.py` (R-3): те же правила нужны `invites.py`, а импорт из `router.py` замкнул бы цикл | #48 PR F |
| `invites.py` | Единственная АНОНИМНАЯ поверхность кабинета: `GET/POST /staff/invite/{token}` — принятие приглашения (форма email+пароль, строка согласия из канона `channels/common/consent.py`), сразу вход и переход в очередь (отказ входа — экран «учётка создана, войдите сами»: откатывать принятие уже нечего); свой роутер, `_page_context` неприменим | #48 PR F |
| `new_request.py` | Данные формы «Новая заявка» (ручной приём, spec 0035 §5): службы отеля чипсами по категориям тенанта, адреса действия и возврата; отель без категорий — подсказка вместо формы | #299 |
| `summary.py` | Данные страницы «Сводка дня» (spec 0035 §6–§7): складывает числа их владельцев — `requests_api.day_summary` и `count_escalations` общего ядра каналов, — раскладывает по плиткам с надписями §9 и строит разрез «По службам»; окно суток для эскалаций берёт из ответа `day_summary`, а не считает второй раз | #300 |
| `queue.py` | Данные страницы очереди: лента открытых / «закрытые за сегодня» (полночь отеля, фолбэк UTC), фильтры категория/«мои» (представление, в памяти), карточки и чипсы, метка «просрочена» по сроку тенанта (spec 0028), бейдж «🚨 срочно» у срочной заявки (spec 0034 §5 — рядом с бейджем состояния, а не вместо: «срочная и просроченная» обязана читаться целиком), плашка «заявка #N создана» после возврата с формы ручного приёма (`created_flash` — номер из URL проходит проверку «цифры, не длиннее пяти») | #48 PR D, #299 |
| `checkin.py` | Данные и расчёты страницы заселения: разбор формы, «12:00 по поясу отеля → UTC» (заезд+ночи и продление), карточка Stay, URL и SVG-QR bind-ссылки (segno); отсюда же берут `hotel_zone`/`format_local` соседние страницы | #48 PR E |
| `team.py` | Данные страницы «Сотрудники»: состав отеля (статус, активность), ожидающие приглашения, URL ссылки-приглашения; подписи и описания ролей всего кабинета (`role_label`, `ROLE_DESCRIPTIONS`) | #48 PR F |
| `rendering.py` | Jinja2-окружение пакета: `render_page(template, **context)`, реестр статики `STATIC_ASSETS` (содержимое, MIME-тип, версия для кэш-бастинга) — новый файл добавляется одной строкой `_STATIC_FILES` | #48 PR C/D/E/F |
| `templates/` | `layout.html` (CANONICAL: каркас страницы) + страницы `login/select_tenant/home/forbidden/queue/new_request/summary/checkin/team/invite/invite_invalid/invite_accepted` и партиалы `_queue_list.html` (его же отдаёт fragment-эндпоинт) и `_stay_card.html` (карточка Stay — контракт селекторов checkin.js в шапке) | #48 PR C/D/E/F, #299, #300 |
| `static/styles.css` | CANONICAL: мобильный CSS-канон кабинета — палитра в `:root` (светлая и тёмная тема одними токенами), тач-таргеты ≥ 44px, отступы под вырез экрана (`env(safe-area-inset-*)`), классы `.card/.form/.btn/.list/.chips` (+чипсы-радио, `.access-code`, `.qr-box` — PR E; `.team-row`, `.invite-url` — PR F; `.badge-overdue`, `.request-card-overdue`, `.hint-alert` — просрочка; `.badge-urgent`, `.request-card-urgent` — срочность, spec 0034; `textarea` — суть заявки в форме ручного приёма, #299; `.tiles`/`.tile`, `.data-table` — плитки и разрез по службам на сводке дня, #300); новые страницы переиспользуют, а не изобретают | #48 PR C/D/E/F, #299, #300 |
| `static/queue.js` | Ванильный JS очереди: поллинг фрагмента каждые 15 с (пауза на скрытой вкладке), POST действий по CSRF-контракту, дружелюбные тексты по кодам ошибок («уже взята»); разметку не рисует — только innerHTML фрагмента | #48 PR D |
| `static/checkin.js` | Ванильный JS карточки Stay: поллинг счётчика привязок каждые 3 с (индикатор «гость подключился»), отсчёт TTL QR, действия по CSRF-контракту; разметку не сочиняет — единственный innerHTML это ГОТОВЫЙ серверный SVG-QR из ответа | #48 PR E |
| `static/new_request.js` | Ванильный JS формы ручного приёма: проверка трёх полей текстами spec 0035 §9, один POST по CSRF-контракту, возврат в очередь с номером созданной заявки; кнопка гаснет на время запроса — второе нажатие завело бы вторую заявку | #299 |
| `static/team.js` | Ванильный JS страницы «Сотрудники»: действия по CSRF-контракту, показ свежей ссылки приглашения (`textContent`) и «поделиться» через Web Share API с фолбэком на буфер обмена; поллинга нет — после действия страница перезагружается | #48 PR F |

## Публичный API

- `router.router` — единственный вход; подключает только composition root
  (`app.py`). Всё остальное — приватные детали пакета.

Маршруты: `GET/POST /staff/login`, `GET /staff/` (выбор отеля по членствам),
`POST /staff/logout`, `GET /staff/{tenant_slug}` (главная, роль любая),
`GET /staff/{tenant_slug}/requests` (+`/fragment`) — очередь (роль любая,
query: `tab=open|closed`, `category`, `mine=1`, `created=N` — плашка о только
что созданной заявке),
`GET /staff/{tenant_slug}/requests/new` — форма ручного приёма (роль любая),
`GET/POST /staff/{tenant_slug}/checkin` — заселение (роль
receptionist/manager; GET `?room=` — поиск карточки, POST — HTML-форма
«Заселить», успех рендерится без redirect: код показывается один раз),
`POST /staff/{tenant_slug}/api/requests` — создать заявку вручную (роль любая;
три поля, `origin=staff_manual`),
`POST /staff/{tenant_slug}/api/requests/{id}/claim|complete|cancel` —
JSON-действия очереди (роль любая; `cancel` требует note),
`POST /staff/{tenant_slug}/api/stays/{id}/bind-link|reissue-code|move|extend|checkout`
и `GET …/stays/{id}/bindings` — JSON-действия карточки Stay (роль
receptionist/manager; выпуск bind-ссылок под rate-limit `ERR-AUTH-010`),
`GET /staff/{tenant_slug}/summary` — сводка дня (роль manager; query:
`day=today|yesterday`, непонятное значение — «сегодня»),
`GET /staff/{tenant_slug}/team` — сотрудники (роль manager),
`POST /staff/{tenant_slug}/api/team/invites`,
`…/team/invites/{id}/revoke`, `…/team/members/{id}/role`,
`…/team/members/{id}/deactivate` — JSON-действия состава (роль manager),
`GET/POST /staff/invite/{token}` — принятие приглашения (БЕЗ сессии),
`GET /staff/static/{filename}` (styles.css, queue.js, new_request.js,
checkin.js, team.js).

## Аутентификация и безопасность

- Cookie сессии — `STAFF_SESSION_COOKIE` (`platform/staff_auth`): HttpOnly +
  Secure + SameSite=Lax + Path=/staff, Max-Age = absolute TTL (контракт
  ревью PR #148). Атрибуты cookie — не граница безопасности: валидность
  перепроверяет сервер на каждом запросе.
- Контекст тенанта для `/staff/{tenant_slug}/…` ставит звено `TenantResolver`
  (`platform/auth.resolve_tenant_from_staff_session`, ADR-008 §6); авторизацию
  действия выполняет страница — канон `_page_context` поверх
  `staff_auth.require_role` (401 → редирект на логин, 403 → HTML «нет
  доступа») — или JSON-действие: `require_role` напрямую (исходы — конверт
  ошибок R-8). Сверка RLS-контекста запроса с тенантом действия живёт в самом
  `require_role` (fail-closed, ревью PR #153) — страницы и JSON-действия под
  одной защитой. Каждая новая страница кабинета копирует канон (§11
  FOUNDATION: эндпоинт рождается аутентифицированным). Единственное
  исключение — `/staff/invite/{token}` (`invites.py`): у держателя ссылки нет
  ни сессии, ни членства, аутентификация там — сам одноразовый токен, а
  «invite» перечислен в `_STAFF_RESERVED_SEGMENTS` резолвера тенанта.
- Роль → право: мини-матрица в `docs/RBAC.md` (она же spec 0033 §3.2). Роль
  доказывает право В ОТЕЛЕ, но не право на объект: смена роли и отключение
  дополнительно требуют активного членства цели в этом же тенанте
  (`ERR-AUTH-008`), отзыв инвайта — его принадлежности тенанту
  (`ERR-AUTH-004`), а свою учётку менеджер не трогает (`ERR-AUTH-011` —
  защита от самоблокировки последнего менеджера; тем же кодом закрыт и третий
  путь смены роли — принятие приглашения действующим менеджером, ревью
  PR #159).
- Дедуп платформенных запросов (ревью PR #153): звено резолвера кладёт готовый
  `StaffContext` в `scope["state"]`, `require_role` того же запроса не
  перечитывает сессию и членство — 2 запроса к БД вместо 4 на каждый
  запрос страницы/поллинга.
- CSRF-контракт (докстринг `router.py`): SameSite=Lax + GET не меняют данных
  (единственный побочный эффект — продление idle-TTL) + проверка `Origin` на
  POST (`browser.is_cross_origin`; отсутствующий Origin допускается только
  для HTML-форм — в том числе для формы принятия приглашения, где сессии ещё
  нет и Origin остаётся единственной браузерной границей login-CSRF).
  JSON-действия (`api_router.py`) требуют
  `Content-Type: application/json` И НЕПУСТОЙ same-origin `Origin` —
  нарушение → 403 `ERR-AUTH-009`; отдельный CSRF-токен не нужен.
- Весь HTML — через `browser.html_page` (`PAGE_HEADERS`): `Cache-Control: no-store`,
  запрет фреймов (X-Frame-Options + CSP frame-ancestors),
  `Referrer-Policy: strict-origin`,
  CSP `default-src 'self'` (свой JS — только файлом из `/staff/static`,
  inline-скрипты и внешние источники запрещены; ревью PR #153).
- **`strict-origin`, а не `no-referrer`** (issue #164): наружу и так уходит
  один источник без пути, поэтому токен приглашения не утекает — а вот
  `no-referrer` заставлял браузер слать формам `Origin: null`, и CSRF-щит
  резал собственные вход, выход, заселение и принятие приглашения. Значение
  закреплено тестом `test_page_referrer_policy_does_not_null_form_origin`;
  `Origin: null` при этом остаётся отказом (подделывается из песочного iframe).
- Rate-limit логина — внутри `staff_auth.login` (канон 0023, по email и IP;
  тратят его только неудачные попытки — issue #207). Адрес клиента даёт канон
  `shared/clientip.py`: за Cloudflare-туннелем `request.client` — это соседний
  контейнер, настоящий адрес приходит заголовком `CF-Connecting-IP` и
  принимается только от прокси из `TRUSTED_PROXY_IPS`.

## События

Не публикует и не потребляет: страницы и действия зовут сервисы `platform/` и
`api.py` модулей (`requests_api.change_request_status` публикует
`request.status_changed` сам — гость получает те же уведомления, что при
действиях из Telegram, P-5; ручной приём зовёт `requests_api.create_request`,
и `request.created` публикует тоже сам модуль — уведомление в чат службы
уходит существующим путём, без второй ветки создания заявки).

## Таблицы

Своих нет. Платформенные таблицы staff-идентичности — только через сервисы
`platform/staff_auth`, `platform/staff_team` (состав отеля) и
`platform/staff_invites` (приглашения) (граница R-5); заявки — только через
`modules/requests/api.py` (кабинет передаёт `acting_user` из `StaffContext`,
«кто взял» пишет сам модуль); Stay и bind-ссылки — только через
`modules/guests/api.py` (заселение, переселение, продление, выезд, QR).

## Зависимости

Внутренние: `hospitality.platform` (staff_auth, staff_credentials, staff_team,
staff_invites, legal — адрес политики, config — часовой пояс отеля),
`hospitality.modules.requests` и `hospitality.modules.guests` (только
`api.py`), `hospitality.shared` (config, errors, logging, db, metrics,
ratelimit), `hospitality.channels.common` — текст согласия (`consent`) и счёт эскалаций
(`events.count_escalations`): сиблинг композиционного слоя, у которого кабинет
берёт то, чем тот владеет, — строку согласия сотрудника нельзя писать заново
(разъехавшиеся тексты — юридический дефект, spec 0029), а таблица эскалаций
живёт там же, где `Conversation` (spec 0035 §6).
Внешние сверх общих для проекта: `jinja2` (шаблоны, ADR-014), `segno`
(серверный SVG-QR bind-ссылки).

## Типовые сценарии изменения

- Новая страница кабинета → маршрут `/{tenant_slug}/…` в `router.py`
  (служебные маршруты login/logout/static/выбор объявлены раньше `/{tenant_slug}`
  — сохранять этот порядок); шаблон наследует `layout.html`; авторизация —
  копия канона `_page_context` с нужными ролями; смоук-тест в `tests/`
  (аутентифицированность, ключевые элементы, редирект без сессии).
- Новое JSON-действие → `api_router.py`: канон — три действия очереди
  (`_authorized_actor` = CSRF-щит + `require_role`), тело — Pydantic-схема,
  переход — через `api.py` модуля с `acting_user`.
- Живое обновление на странице → канон очереди: партиал списка + fragment-
  эндпоинт + поллинг из отдельного JS-файла (CSP запрещает inline);
  никакой разметки в JS.
- Правка стилей/JS → только файлы `static/` (кэш обновится сам — версия в
  URL из `rendering.py`); НОВЫЙ файл статики → строка в `_STATIC_FILES`
  (`rendering.py`), маршрут отдачи один на всех; новые CSS-классы — только
  если канонические не подходят, с комментарием зачем.
- Новый ЦВЕТ в стилях → переменная в `:root` И её значение в блоке
  `prefers-color-scheme: dark`: цвет литералом в правиле расходится с тёмной
  темой молча. Исключений в файле нет: белая подложка QR — не CSS, segno рисует
  её внутрь самой картинки (`checkin.qr_svg`, `light="#ffffff"`).
- Тёмная тема не заканчивается палитрой: `color-scheme: light dark` в `:root`
  сообщает её браузеру, иначе он подмешивает своё светлое (подложка резинового
  скролла в iOS, плашка автозаполнения поверх полей входа, выпадающие списки).
- Текст поверх заливки акцентом — только `var(--on-accent)`: в тёмной теме
  акцент светлеет, и белым по нему уже не прочитать.
- Новое право или роль → сначала `docs/RBAC.md` (мини-матрица), затем
  `require_role(...)` У СТРАНИЦЫ И У ЕЁ JSON-ДЕЙСТВИЙ одновременно: страница
  без действия (или наоборот) — дыра.
- Новый служебный сегмент под `/staff/` (не slug тенанта) → добавить в
  `_STAFF_RESERVED_SEGMENTS` (`platform/auth.py`) в том же PR.
