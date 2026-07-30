# staff_portal — кабинет персонала (composition-слой)

## Назначение

Server-rendered веб-кабинет персонала отеля (spec 0033, ADR-014): очередь
заявок, заселение с QR, управление сотрудниками. Условие запуска пилота
(issue #48). Слой композиции (FOUNDATION §5.1 «Web (персонал/админ)»):
импортирует `platform/` и `api.py` доменных модулей; сам не импортируется
никем, кроме composition root (контракт 7 import-linter). PR C дал каркас
(вход/выход, выбор отеля, layout и CSS-канон), PR D — очередь заявок
(закрывает #56); экраны заселения и сотрудников — PR E, PR F.

## Состав

| Файл | Что даёт | Задача |
| --- | --- | --- |
| `router.py` | Страницы: логин/логаут, выбор отеля, главная `/staff/{tenant_slug}`, очередь `…/requests` (+fragment для поллинга), отдача статики; канон `_page_context` (авторизация страницы с браузерными исходами) и CSRF-контракт (докстринг модуля); подключает `api_router` | #48 PR C/D |
| `api_router.py` | JSON-действия очереди: взять/готово/отменить — `require_role` напрямую, CSRF-щит `Content-Type: application/json` + непустой same-origin `Origin` (403 `ERR-AUTH-009`) | #48 PR D |
| `queue.py` | Данные страницы очереди: лента открытых / «закрытые за сегодня» (полночь отеля, фолбэк UTC), фильтры категория/«мои» (представление, в памяти), карточки и чипсы | #48 PR D |
| `rendering.py` | Jinja2-окружение пакета: `render_page(template, **context)`, версии статики для кэш-бастинга (`STYLES_VERSION`, `QUEUE_JS_VERSION`) | #48 PR C/D |
| `templates/` | `layout.html` (CANONICAL: каркас страницы) + страницы `login/select_tenant/home/forbidden/queue` и партиал `_queue_list.html` (его же отдаёт fragment-эндпоинт — разметка карточки живёт в одном месте) | #48 PR C/D |
| `static/styles.css` | CANONICAL: мобильный CSS-канон кабинета — палитра в `:root`, тач-таргеты ≥ 44px, классы `.card/.form/.btn/.list/.chips`; новые страницы переиспользуют, а не изобретают | #48 PR C/D |
| `static/queue.js` | Ванильный JS очереди: поллинг фрагмента каждые 15 с (пауза на скрытой вкладке), POST действий по CSRF-контракту, дружелюбные тексты по кодам ошибок («уже взята»); разметку не рисует — только innerHTML фрагмента | #48 PR D |

## Публичный API

- `router.router` — единственный вход; подключает только composition root
  (`app.py`). Всё остальное — приватные детали пакета.

Маршруты: `GET/POST /staff/login`, `GET /staff/` (выбор отеля по членствам),
`POST /staff/logout`, `GET /staff/{tenant_slug}` (главная, роль любая),
`GET /staff/{tenant_slug}/requests` (+`/fragment`) — очередь (роль любая,
query: `tab=open|closed`, `category`, `mine=1`),
`POST /staff/{tenant_slug}/api/requests/{id}/claim|complete|cancel` —
JSON-действия (роль любая; `cancel` требует note, `complete` — опционально),
`GET /staff/static/styles.css`, `GET /staff/static/queue.js`.

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
  FOUNDATION: эндпоинт рождается аутентифицированным).
- Дедуп платформенных запросов (ревью PR #153): звено резолвера кладёт готовый
  `StaffContext` в `scope["state"]`, `require_role` того же запроса не
  перечитывает сессию и членство — 2 запроса к БД вместо 4 на каждый
  запрос страницы/поллинга.
- CSRF-контракт (докстринг `router.py`): SameSite=Lax + GET не меняют данных
  (единственный побочный эффект — продление idle-TTL) + проверка `Origin` на
  POST (`_is_cross_origin`; отсутствующий Origin допускается только для
  HTML-форм). JSON-действия (`api_router.py`) требуют
  `Content-Type: application/json` И НЕПУСТОЙ same-origin `Origin` —
  нарушение → 403 `ERR-AUTH-009`; отдельный CSRF-токен не нужен.
- Весь HTML — через `_html_page` (`_PAGE_HEADERS`): `Cache-Control: no-store`,
  запрет фреймов (X-Frame-Options + CSP frame-ancestors), no-referrer,
  CSP `default-src 'self'` (свой JS — только файлом из `/staff/static`,
  inline-скрипты и внешние источники запрещены; ревью PR #153).
- Rate-limit логина — внутри `staff_auth.login` (канон 0023, по email и IP).
  `client_ip` берётся из `request.client` — за реверс-прокси/туннелем это
  адрес прокси, пока uvicorn не запущен с `--proxy-headers` (заметка в #149).

## События

Не публикует и не потребляет: страницы и действия зовут сервисы `platform/` и
`api.py` модулей (`requests_api.change_request_status` публикует
`request.status_changed` сам — гость получает те же уведомления, что при
действиях из Telegram, P-5).

## Таблицы

Своих нет. Платформенные таблицы staff-идентичности — только через сервисы
`platform/staff_auth` (граница R-5); заявки — только через
`modules/requests/api.py` (кабинет передаёт `acting_user` из `StaffContext`,
«кто взял» пишет сам модуль).

## Зависимости

Внутренние: `hospitality.platform` (staff_auth, staff_credentials, config —
часовой пояс отеля для «закрытых за сегодня»), `hospitality.modules.requests`
(только `api.py`), `hospitality.shared` (config, errors, logging, db).
Внешние сверх общих для проекта: `jinja2` (шаблоны, ADR-014).

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
  URL из `rendering.py`); новые CSS-классы — только если канонические не
  подходят, с комментарием зачем.
- Новый служебный сегмент под `/staff/` (не slug тенанта) → добавить в
  `_STAFF_RESERVED_SEGMENTS` (`platform/auth.py`) в том же PR.
