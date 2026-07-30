# staff_portal — кабинет персонала (composition-слой)

## Назначение

Server-rendered веб-кабинет персонала отеля (spec 0033, ADR-014): очередь
заявок, заселение с QR, управление сотрудниками. Условие запуска пилота
(issue #48). Слой композиции (FOUNDATION §5.1 «Web (персонал/админ)»):
импортирует `platform/` и `api.py` доменных модулей; сам не импортируется
никем, кроме composition root (контракт 7 import-linter). PR C серии даёт
каркас: вход/выход, выбор отеля, layout и мобильный CSS-канон; экраны —
PR D (очередь), PR E (заселение), PR F (сотрудники).

## Состав

| Файл | Что даёт | Задача |
| --- | --- | --- |
| `router.py` | Страницы: логин/логаут, выбор отеля, главная кабинета `/staff/{tenant_slug}`, отдача CSS; канон `_page_context` (авторизация страницы с браузерными исходами) и CSRF-контракт (докстринг модуля) | #48 PR C |
| `rendering.py` | Jinja2-окружение пакета: `render_page(template, **context)`, `STYLES_VERSION` для кэш-бастинга CSS | #48 PR C |
| `templates/` | `layout.html` (CANONICAL: каркас страницы — шапка, «кто вошёл», Выйти) + страницы `login/select_tenant/home/forbidden` | #48 PR C |
| `static/styles.css` | CANONICAL: мобильный CSS-канон кабинета — палитра в `:root`, тач-таргеты ≥ 44px, классы `.card/.form/.btn/.list`; новые страницы переиспользуют, а не изобретают | #48 PR C |
| `api_router.py` | JSON-действия (взять/готово/заселить) — появится в PR D | #48 PR D |

## Публичный API

- `router.router` — единственный вход; подключает только composition root
  (`app.py`). Всё остальное — приватные детали пакета.

Маршруты: `GET/POST /staff/login`, `GET /staff/` (выбор отеля по членствам),
`POST /staff/logout`, `GET /staff/{tenant_slug}` (главная, роль любая),
`GET /staff/static/styles.css`.

## Аутентификация и безопасность

- Cookie сессии — `STAFF_SESSION_COOKIE` (`platform/staff_auth`): HttpOnly +
  Secure + SameSite=Lax + Path=/staff, Max-Age = absolute TTL (контракт
  ревью PR #148). Атрибуты cookie — не граница безопасности: валидность
  перепроверяет сервер на каждом запросе.
- Контекст тенанта для `/staff/{tenant_slug}/…` ставит звено `TenantResolver`
  (`platform/auth.resolve_tenant_from_staff_session`, ADR-008 §6); авторизацию
  действия выполняет страница — канон `_page_context` поверх
  `staff_auth.require_role` (401 → редирект на логин, 403 → HTML «нет
  доступа»). Каждая новая страница кабинета копирует этот канон (§11
  FOUNDATION: эндпоинт рождается аутентифицированным).
- CSRF-контракт (докстринг `router.py`): SameSite=Lax + все GET без побочных
  эффектов + проверка `Origin` на POST (`_is_cross_origin`). Будущие
  JSON-действия (PR D+) обязаны требовать `Content-Type: application/json`
  и проходить ту же проверку Origin; отдельный CSRF-токен при этом не нужен.
- Rate-limit логина — внутри `staff_auth.login` (канон 0023, по email и IP).
  `client_ip` берётся из `request.client` — за реверс-прокси/туннелем это
  адрес прокси, пока uvicorn не запущен с `--proxy-headers` (заметка в #149).

## События

Не публикует и не потребляет: страницы зовут сервисы `platform/` (и с PR D —
`api.py` модулей), события остаются за сервисами.

## Таблицы

Своих нет. Читает/пишет платформенные таблицы staff-идентичности только через
сервисы `platform/staff_auth` (граница R-5).

## Зависимости

Внутренние: `hospitality.platform` (staff_auth: login/logout/сессии/
`require_role`/`list_memberships`), `hospitality.shared` (config, errors,
logging). С PR D добавятся `api.py` доменных модулей (requests, guests).
Внешние сверх общих для проекта: `jinja2` (шаблоны, ADR-014).

## Типовые сценарии изменения

- Новая страница кабинета → маршрут `/{tenant_slug}/…` в `router.py`
  (служебные маршруты login/logout/static/выбор объявлены раньше `/{tenant_slug}`
  — сохранять этот порядок); шаблон наследует `layout.html`; авторизация —
  копия канона `_page_context` с нужными ролями; смоук-тест в
  `tests/test_pages.py` (аутентифицированность, ключевые элементы, редирект
  без сессии).
- Новое JSON-действие (PR D+) → `api_router.py`: `require_role` напрямую
  (JSON-исходы), `Content-Type: application/json` + Origin-проверка (CSRF).
- Правка стилей → только `static/styles.css` (кэш обновится сам — версия в
  URL из `STYLES_VERSION`); новые классы — только если канонические не
  подходят, с комментарием зачем.
- Новый служебный сегмент под `/staff/` (не slug тенанта) → добавить в
  `_STAFF_RESERVED_SEGMENTS` (`platform/auth.py`) в том же PR.
