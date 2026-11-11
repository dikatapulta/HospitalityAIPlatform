"""Рендеринг страниц кабинета — Jinja2 из файлов пакета (spec 0033 §4, ADR-014).

Шаблоны — `templates/` (общий каркас `layout.html`, страницы наследуют его),
статика — `static/` (мобильный CSS-канон кабинета и ванильный JS страниц).
Автоэкранирование включено — имена гостей/отелей/сотрудников попадают в HTML
безопасно.

Статика читается один раз при импорте и отдаётся из памяти (`router.py`);
версия — хэш содержимого в query-параметре ссылки из шаблона: браузер кэширует
файл надолго, а любое его изменение меняет URL. JS — отдельный файл, не
inline-`<script>`: CSP кабинета `default-src 'self'` inline-скрипты запрещает
(рекомендация ревью PR #153).

Новый файл статики — одна строка в `_STATIC_FILES`: отдачу и кэш-бастинг он
получает автоматически, отдельного маршрута заводить не нужно (PR F).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, select_autoescape

_PACKAGE_DIR: Final = Path(__file__).resolve().parent

_CSS: Final = "text/css; charset=utf-8"
_JS: Final = "text/javascript; charset=utf-8"
# Шрифт кабинета Golos Text (issue #635, лицензия OFL — static/golos-text-OFL.txt):
# свой файл, а не Google Fonts — CSP `default-src 'self'` чужие шрифты не пускает,
# и Google не узнаёт о каждом открытии кабинета.
_FONT: Final = "font/woff2"
# Иконки и манифест веб-приложения на экране «Домой» (spec 0050 §3).
_PNG: Final = "image/png"
_SVG: Final = "image/svg+xml"
# Стоковые фото демо «Забытые вещи» (#794).
_WEBP: Final = "image/webp"
_MANIFEST: Final = "application/manifest+json"
_BINARY_TYPES: Final = frozenset({_FONT, _PNG, _WEBP})

# Единственный список статики кабинета: файл, MIME-тип, имя переменной шаблона
# с версией для кэш-бастинга (`href=…?v={{ styles_version }}`).
_STATIC_FILES: Final[tuple[tuple[str, str, str], ...]] = (
    # Тема кабинета (#635): шрифт, палитра светлой и тёмной темы, шкала — первым.
    ("theme.css", _CSS, "theme_css_version"),
    ("styles.css", _CSS, "styles_version"),
    ("queue.js", _JS, "queue_js_version"),
    # Действия пути номера (spec 0046 §5): общие для «Выездов и уборки» и «Номеров».
    ("turnover_actions.js", _JS, "turnover_actions_js_version"),
    ("turnovers.js", _JS, "turnovers_js_version"),
    # Доска «Номера» (spec 0061 §4): клетки, фильтры, панель номера, поллинг.
    ("room_board.css", _CSS, "room_board_css_version"),
    ("room_board.js", _JS, "room_board_js_version"),
    ("floors.js", _JS, "floors_js_version"),
    # «Выезды и уборка» и «Этажи» (кабинет v3, #639): раскладка двух страниц.
    ("housekeeping.css", _CSS, "housekeeping_css_version"),
    ("act_requests.js", _JS, "act_requests_js_version"),
    ("act_requests.css", _CSS, "act_requests_css_version"),
    # Панель «Товары и меню» (spec 0038 §6 PR 4): своя раскладка и скрипт.
    ("catalog.css", _CSS, "catalog_css_version"),
    ("catalog.js", _JS, "catalog_js_version"),
    # Поле «Фото» формы товара (PR 7): выбор, предпросмотр, загрузка.
    ("catalog_photo.js", _JS, "catalog_photo_js_version"),
    # «Наставник» (spec 0048 §10 PR 4): чат с потоком, кодекс и статистика.
    ("mentor.css", _CSS, "mentor_css_version"),
    ("mentor_chat.js", _JS, "mentor_chat_js_version"),
    ("mentor_exam.js", _JS, "mentor_exam_js_version"),
    ("mentor_results.js", _JS, "mentor_results_js_version"),
    ("mentor_codex.js", _JS, "mentor_codex_js_version"),
    # «Неточность в статье» (spec 0072 §6): кнопка у статьи «Кодекса» и под проверкой ответа.
    ("mentor_flags.js", _JS, "mentor_flags_js_version"),
    ("ideas.js", _JS, "ideas_js_version"),
    # «Служебные записки» (spec 0077 §4): ключ устройства, «Моя подпись», плашка о привязке.
    ("passkey.js", _JS, "passkey_js_version"),
    ("memo_signature.js", _JS, "memo_signature_js_version"),
    ("memo_confirm.js", _JS, "memo_confirm_js_version"),
    ("memos.js", _JS, "memos_js_version"),
    ("passkey_notice.js", _JS, "passkey_notice_js_version"),
    ("memos.css", _CSS, "memos_css_version"),
    ("checkin.js", _JS, "checkin_js_version"),
    # Страница заселения (кабинет v3, #641): форма, панели и карточка комнаты.
    ("checkin.css", _CSS, "checkin_css_version"),
    # «Сотрудники»: общее приглашения по одному и списком (spec 0072 §4) — первым.
    ("team_common.js", _JS, "team_common_js_version"),
    ("team.js", _JS, "team_js_version"),
    ("team_batch.js", _JS, "team_batch_js_version"),
    ("team.css", _CSS, "team_css_version"),
    ("new_request.js", _JS, "new_request_js_version"),
    # Очередь заявок и страница «Новая заявка» (кабинет v3, #638): строки, вкладки, поиск.
    ("queue.css", _CSS, "queue_css_version"),
    ("knowledge.js", _JS, "knowledge_js_version"),
    # «Вид гостевого чата» (spec 0066 §6): варианты, предпросмотр, загрузка знака.
    ("chat_look.css", _CSS, "chat_look_css_version"),
    ("chat_look.js", _JS, "chat_look_js_version"),
    ("demo_handover.css", _CSS, "demo_handover_css_version"),
    ("demo_handover.js", _JS, "demo_handover_js_version"),
    ("demo_handover_board.css", _CSS, "demo_handover_board_css_version"),
    ("demo_handover_board.js", _JS, "demo_handover_board_js_version"),
    # Демо «Забытые вещи» (#604): поиск, форма и выдача — только в браузере.
    ("demo_lost_found.css", _CSS, "demo_lost_found_css_version"),
    ("demo_lost_found.js", _JS, "demo_lost_found_js_version"),
    # Фото выдуманных находок (#794): стоковые, источники — demo_lost_found_SOURCES.md;
    # адрес с версией собирает demo_lost_found._photo_url.
    ("demo_lost_found_charger.webp", _WEBP, "demo_lost_found_charger_version"),
    ("demo_lost_found_headphones.webp", _WEBP, "demo_lost_found_headphones_version"),
    ("demo_lost_found_glasses.webp", _WEBP, "demo_lost_found_glasses_version"),
    ("demo_lost_found_toy.webp", _WEBP, "demo_lost_found_toy_version"),
    ("demo_lost_found_watch.webp", _WEBP, "demo_lost_found_watch_version"),
    ("demo_lost_found_scarf.webp", _WEBP, "demo_lost_found_scarf_version"),
    ("demo_lost_found_umbrella.webp", _WEBP, "demo_lost_found_umbrella_version"),
    ("demo_lost_found_book.webp", _WEBP, "demo_lost_found_book_version"),
    ("demo_lost_found_cosmetics.webp", _WEBP, "demo_lost_found_cosmetics_version"),
    ("demo_lost_found_thermos.webp", _WEBP, "demo_lost_found_thermos_version"),
    # «Тестовые данные» (#740): кнопка демо-картины на staging демо-отеля.
    ("demo_data.js", _JS, "demo_data_js_version"),
    # «3D-витрина» (spec 0069 §2.2): загрузка сцены и страница посетителя — менеджеру;
    # просмотрщик и панель — статика канала `/showroom/static`.
    ("showroom_admin.js", _JS, "showroom_admin_js_version"),
    # «Диалоги» (spec 0067, #767): список веб-диалогов и лента гостя с ботом.
    ("conversations.css", _CSS, "conversations_css_version"),
    # «Отчёт» (spec 0083 §2.4): лист для печати и кнопка печати браузера.
    ("report.css", _CSS, "report_css_version"),
    ("report.js", _JS, "report_js_version"),
    ("signal.js", _JS, "signal_js_version"),
    # Веб-пуш (spec 0050): кнопка и подписка телефона; service worker отдаёт
    # свой маршрут `/staff/sw.js` — его область обязана покрывать весь `/staff/`.
    ("push.js", _JS, "push_js_version"),
    ("sw.js", _JS, "sw_js_version"),
    # Манифест ссылается на иконки без версии в URL, как шрифты в shell.css:
    # новая картинка — новое имя файла.
    ("manifest.webmanifest", _MANIFEST, "manifest_version"),
    ("icon-192.png", _PNG, "icon_192_version"),
    ("icon-512.png", _PNG, "icon_512_version"),
    ("apple-touch-icon.png", _PNG, "apple_touch_icon_version"),
    ("badge-96.png", _PNG, "badge_96_version"),
    ("home.css", _CSS, "home_css_version"),
    # Главная v3 (#635, PR 2): вкладки «Требует внимания» и «Взять себе».
    ("home.js", _JS, "home_js_version"),
    ("shell.css", _CSS, "shell_css_version"),
    # Каркас v3 (#635): тема до первой отрисовки, меню профиля, «Ещё» телефона.
    ("shell.js", _JS, "shell_js_version"),
    # Окна каркаса (#636): «Новая заявка» и карточка заявки поверх страницы, всплывашка.
    ("app_windows.js", _JS, "app_windows_js_version"),
    # Страницы до входа (#637): раскладка и кнопка «Показать пароль».
    ("auth.css", _CSS, "auth_css_version"),
    ("auth.js", _JS, "auth_js_version"),
    # Стрелка выпадающего списка (styles.css): файлом, а не data: — CSP
    # `default-src 'self'` картинки data: не пускает.
    ("select-chevron.svg", _SVG, "select_chevron_version"),
    # Шрифт подключает @font-face в shell.css без версии в URL: файл кэшируется
    # навсегда, поэтому новая версия шрифта — новое имя файла.
    ("golos-text-latin.woff2", _FONT, "golos_latin_version"),
    ("golos-text-latin-ext.woff2", _FONT, "golos_latin_ext_version"),
    ("golos-text-cyrillic.woff2", _FONT, "golos_cyrillic_version"),
    ("golos-text-cyrillic-ext.woff2", _FONT, "golos_cyrillic_ext_version"),
)


@dataclass(frozen=True)
class StaticAsset:
    """Файл статики в памяти: содержимое, MIME-тип и версия (хэш содержимого).

    Текст (CSS, JS, манифест) — строкой, шрифт и картинки — байтами."""

    content: str | bytes
    media_type: str
    version: str


def _load(filename: str, media_type: str) -> StaticAsset:
    raw = (_PACKAGE_DIR / "static" / filename).read_bytes()
    content: str | bytes = raw if media_type in _BINARY_TYPES else raw.decode("utf-8")
    return StaticAsset(
        content=content,
        media_type=media_type,
        version=hashlib.sha256(raw).hexdigest()[:8],
    )


STATIC_ASSETS: Final[dict[str, StaticAsset]] = {
    filename: _load(filename, media_type) for filename, media_type, _ in _STATIC_FILES
}

_environment: Final = Environment(
    loader=FileSystemLoader(_PACKAGE_DIR / "templates"),
    autoescape=select_autoescape(("html",)),
)
for _filename, _, _version_variable in _STATIC_FILES:
    _environment.globals[_version_variable] = STATIC_ASSETS[_filename].version


def render_page(template_name: str, **context: Any) -> str:
    """HTML страницы кабинета по шаблону из `templates/`."""
    return _environment.get_template(template_name).render(**context)
