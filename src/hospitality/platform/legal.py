"""Публикация политики конфиденциальности (spec 0029 §2, юрпакет П1.2).

Текст согласия обязан содержать РАБОЧУЮ ссылку на политику (ст. 8 Закона РК
«О персональных данных»; `docs/legal/consent-text.md`), поэтому политику
публикует сама платформа: `GET /legal/privacy`.

- Роут публичный и вне контекста тенанта — явное решение §11, как `/health`
  и `/metrics`: оператор обработки один на инсталляцию (владелец платформы,
  а не отель), политика у него одна.
- Страница отеля `GET /legal/privacy/{slug}` (spec 0029 §11) — тот же текст с
  абзацем о собственнике базы данных: наименование и БИН/ИИН юрлица отеля из
  настройки `data_owner` обязан называть состав согласия (Закон № 94-V, ст. 8
  п. 4 пп. 1). На неё ведёт ссылка с экрана согласия; общая страница абзац не
  публикует.
- Источник — `docs/legal/privacy-policy.md`: один источник истины, правка
  политики юристом не требует правки кода. В образ файл кладёт `COPY docs/legal`
  (ops/Dockerfile).
- Публикуемая часть — всё после ПЕРВОЙ строки `---`: до неё в документах
  `docs/legal/` живёт служебная преамбула (статус, что показать юристу),
  гостю она не предназначена. Правило записано и в самом документе.
- Рендер — минимальное подмножество Markdown без зависимостей, как страница
  веб-чата (`channels/web/page.py`, R-3/P-1): заголовки, списки, `**жирный**`,
  цитаты, `---`.

Второй документ — политика 3D-витрины (`GET /legal/showroom-privacy`, spec 0069
§2.6): посетитель витрины — не гость, у него своя короткая политика. Те же
правила публикации и рендера, другой файл.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from hospitality.platform.config import (
    TENANT_NOT_CONFIGURED_ERROR_CODE,
    DataOwner,
    load_tenant_config,
)
from hospitality.platform.models import Tenant
from hospitality.shared.config import Settings, get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

router = APIRouter(prefix="/legal", tags=["legal"])

# Путь роута — единственное место истины: из него собирается ссылка в тексте
# согласия (`privacy_policy_url`), им же объявлен маршрут ниже.
PRIVACY_POLICY_PATH = "/legal/privacy"
# Политика витрины (spec 0069 §2.6): ссылка в строке согласия под формами витрины.
SHOWROOM_PRIVACY_PATH = "/legal/showroom-privacy"

# Код каталога ошибок (docs/runbooks/errors.md, R-8): документ политики не
# найден или пуст. Блокер, а не косметика: ссылка в согласии ведёт в ошибку.
ERR_PRIVACY_POLICY_UNAVAILABLE = "ERR-PLATFORM-008"

_POLICY_RELATIVE_PATH = Path("docs") / "legal" / "privacy-policy.md"
_SHOWROOM_RELATIVE_PATH = Path("docs") / "legal" / "showroom-privacy.md"
# Разделитель служебной преамбулы и публикуемой части (см. докстринг модуля).
_PUBLIC_PART_MARKER = "\n---\n"

# Места наименования и БИН/ИИН отеля в абзаце о собственнике (§1 политики на каждом
# языке). Формулировка абзаца живёт в документе; код подставляет только значения.
_OWNER_NAME_PLACEHOLDER = "{hotel_legal_name}"
_OWNER_BIN_PLACEHOLDER = "{hotel_bin}"
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")

_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")


def privacy_policy_url(settings: Settings | None = None, *, tenant_slug: str | None = None) -> str:
    """Абсолютный URL политики для текста согласия (spec 0029 §2).

    Абсолютный, а не путь: текст согласия уходит в Telegram, где относительная
    ссылка бессмысленна. Хост — `PUBLIC_BASE_URL` (та же переменная, которой
    деплой регистрирует вебхук). С `tenant_slug` — страница отеля: она называет
    собственника базы данных (spec 0029 §11).
    """
    base = (settings or get_settings()).public_base_url.rstrip("/")
    if tenant_slug:
        return f"{base}{PRIVACY_POLICY_PATH}/{quote(tenant_slug, safe='')}"
    return f"{base}{PRIVACY_POLICY_PATH}"


async def load_data_owner(tenant_slug: str) -> DataOwner | None:
    """Собственник базы данных гостей по адресу отеля; `None` — назвать некого.

    `None` — и неизвестный адрес, и отель без конфигурации, и отель без
    `data_owner`: страница отдаёт общий текст, без абзаца о собственнике. 404 на
    чужой адрес незачем — ссылку строит код, а политика нужна гостю в любом
    случае. Отель без `data_owner` — пробел настройки (согласие по составу
    неполно), его называет WARNING.
    """
    async with platform_session_scope() as session:
        tenant_id = await session.scalar(select(Tenant.id).where(Tenant.slug == tenant_slug))
        if tenant_id is None:
            return None
        try:
            config = await load_tenant_config(session, tenant_id)
        except AppError as error:
            # Сломанный конфиг не должен ронять страницу, на которую ведёт согласие.
            if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
                logger.warning(
                    "privacy_policy_tenant_config_unavailable",
                    tenant_id=str(tenant_id),
                    error_code=error.code,
                )
            return None
    if config.data_owner is None:
        logger.warning("privacy_policy_data_owner_missing", tenant_id=str(tenant_id))
    return config.data_owner


def _with_data_owner(markdown: str, owner: DataOwner | None) -> str:
    """Абзац о собственнике: со значениями отеля или без абзаца вовсе.

    Абзац — группа строк между пустыми; тот, где есть место для наименования или
    БИН, публикуется только с обоими значениями — гость не должен видеть ни
    пустое место, ни фигурные скобки.
    """
    kept: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(markdown):
        if _OWNER_NAME_PLACEHOLDER not in paragraph and _OWNER_BIN_PLACEHOLDER not in paragraph:
            kept.append(paragraph)
        elif owner is not None:
            kept.append(
                paragraph.replace(_OWNER_NAME_PLACEHOLDER, owner.name).replace(
                    _OWNER_BIN_PLACEHOLDER, owner.bin_iin
                )
            )
    return "\n\n".join(kept)


def _candidate_paths(relative: Path = _POLICY_RELATIVE_PATH) -> tuple[Path, ...]:
    """Где искать документ политики: репозиторий разработчика, затем рабочая
    директория процесса.

    Два кандидата вместо одного — потому что в образе пакет установлен в
    site-packages (`pip install .`), а `docs/legal/` лежит рядом с рабочей
    директорией `/app` (COPY в Dockerfile): путь «от `__file__` вверх» там не
    работает, а относительный от CWD — работает. Локально и в тестах верно
    обратное направление, поэтому проверяются оба.
    """
    repo_root = Path(__file__).resolve().parents[3]
    return (repo_root / relative, Path.cwd() / relative)


def load_privacy_policy_markdown() -> str:
    """Публикуемая часть политики (Markdown); кэш — файл не меняется без рестарта."""
    return _load_public_markdown(_POLICY_RELATIVE_PATH)


def load_showroom_privacy_markdown() -> str:
    """Публикуемая часть политики 3D-витрины (spec 0069 §2.6)."""
    return _load_public_markdown(_SHOWROOM_RELATIVE_PATH)


@lru_cache
def _load_public_markdown(relative: Path) -> str:
    for candidate in _candidate_paths(relative):
        try:
            raw = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        _, marker, public_part = raw.partition(_PUBLIC_PART_MARKER)
        text = (public_part if marker else raw).strip()
        if text:
            return text
    logger.error(
        "privacy_policy_unavailable",
        error_code=ERR_PRIVACY_POLICY_UNAVAILABLE,
        candidates=[str(path) for path in _candidate_paths(relative)],
    )
    raise AppError(
        code=ERR_PRIVACY_POLICY_UNAVAILABLE,
        message="Текст политики конфиденциальности недоступен",
        status_code=500,
    )


def _render_block(line: str) -> str:
    """Одиночная строка-блок Markdown → HTML (заголовок, разделитель, цитата)."""
    if line == "---":
        return "<hr>"
    if line.startswith("### "):
        return f"<h3>{_inline(line[4:])}</h3>"
    if line.startswith("## "):
        return f"<h2>{_inline(line[3:])}</h2>"
    if line.startswith("# "):
        return f"<h1>{_inline(line[2:])}</h1>"
    if line.startswith("> "):
        return f"<blockquote>{_inline(line[2:])}</blockquote>"
    return f"<p>{_inline(line)}</p>"  # pragma: no cover — сюда попадают только блоки выше


def _inline(text: str) -> str:
    """Экранирование + `**жирный**`; ссылок в политике нет — разметки нет."""
    return _BOLD_PATTERN.sub(r"<strong>\1</strong>", html.escape(text))


def render_privacy_policy(owner: DataOwner | None = None) -> str:
    """HTML страницы политики гостевого чата; с `owner` — страница отеля (§11)."""
    return _render_policy(
        _with_data_owner(load_privacy_policy_markdown(), owner),
        title="Privacy policy · Политика конфиденциальности",
    )


def render_showroom_privacy() -> str:
    """HTML страницы политики 3D-витрины (spec 0069 §2.6)."""
    return _render_policy(
        load_showroom_privacy_markdown(),
        title="3D showroom privacy policy · Политика конфиденциальности 3D-витрины",
    )


def _render_policy(markdown: str, *, title: str) -> str:
    """HTML страницы политики.

    Абзац = группа непустых строк (Markdown переносит длинные абзацы), поэтому
    строки склеиваются до пустой строки, а уже потом превращаются в блок.
    """
    blocks: list[str] = []
    paragraph: list[str] = []
    item: list[str] = []
    in_list = False

    def close_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        # `<li>` вне `<ul>` браузеры рисуют, но это невалидный HTML.
        nonlocal in_list
        if item:
            blocks.append(f"<li>{_inline(' '.join(item))}</li>")
            item.clear()
        if in_list:
            blocks.append("</ul>")
            in_list = False

    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            close_paragraph()
            close_list()
            continue
        if stripped.startswith("- "):
            close_paragraph()
            if item:
                blocks.append(f"<li>{_inline(' '.join(item))}</li>")
                item.clear()
            if not in_list:
                blocks.append("<ul>")
                in_list = True
            item.append(stripped[2:])
            continue
        if stripped.startswith(("#", "> ")) or stripped == "---":
            close_paragraph()
            close_list()
            blocks.append(_render_block(stripped))
            continue
        if in_list:
            # Продолжение пункта: Markdown переносит длинные пункты на след. строку.
            item.append(stripped)
            continue
        paragraph.append(stripped)
    close_paragraph()
    close_list()
    return _PAGE_TEMPLATE.format(title=html.escape(title), body="\n".join(blocks))


_PAGE_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  /* Цвета — палитра гостевых страниц «Небо и золото» (channels/web/assets/tokens.css,
     #746); копия, потому что ядро не импортирует каналы. */
  :root {{ --bg:#f2f6fa; --text:#122033; --muted:#536477; --line:#d5e0eb; --accent:#1f6fb2;
          color-scheme:light; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0c1625; --text:#e8eef6; --muted:#9aa9bc; --line:#2c384a; --accent:#6fb2ea;
            color-scheme:dark; }}
  }}
  body {{ font:16px/1.55 system-ui,sans-serif; color:var(--text); background:var(--bg);
         max-width:44rem; margin:0 auto; padding:24px 16px 64px; }}
  a {{ color:var(--accent); }}
  h1 {{ font-size:22px; margin:24px 0 8px; }}
  h2 {{ font-size:19px; margin:28px 0 8px; }}
  h3 {{ font-size:17px; margin:20px 0 6px; }}
  p, li {{ margin:8px 0; }}
  ul {{ padding-left:0; }}
  li {{ margin-left:20px; }}
  blockquote {{ margin:8px 0; padding-left:12px; border-left:3px solid var(--line);
                color:var(--muted); }}
  hr {{ border:0; border-top:1px solid var(--line); margin:28px 0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy_page() -> HTMLResponse:
    """Политика конфиденциальности гостевого чата (kk/ru/en) — публичная страница."""
    return HTMLResponse(render_privacy_policy())


@router.get("/privacy/{tenant_slug}", response_class=HTMLResponse)
async def hotel_privacy_policy_page(tenant_slug: str) -> HTMLResponse:
    """Политика гостевого чата с собственником базы данных — страница отеля (§11)."""
    return HTMLResponse(render_privacy_policy(await load_data_owner(tenant_slug)))


@router.get("/showroom-privacy", response_class=HTMLResponse)
async def showroom_privacy_page() -> HTMLResponse:
    """Политика 3D-витрины (en/ru/kk) — публичная страница (spec 0069 §2.6)."""
    return HTMLResponse(render_showroom_privacy())
