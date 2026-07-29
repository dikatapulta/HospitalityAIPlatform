"""Публикация политики конфиденциальности (spec 0029 §2, юрпакет П1.2).

Текст согласия обязан содержать РАБОЧУЮ ссылку на политику (ст. 8 Закона РК
«О персональных данных»; `docs/legal/consent-text.md`), поэтому политику
публикует сама платформа: `GET /legal/privacy`.

- Роут публичный и вне контекста тенанта — явное решение §11, как `/health`
  и `/metrics`: оператор обработки один на инсталляцию (владелец платформы,
  а не отель), политика у него одна.
- Источник — `docs/legal/privacy-policy.md`: один источник истины, правка
  политики юристом не требует правки кода. В образ файл кладёт `COPY docs/legal`
  (ops/Dockerfile).
- Публикуемая часть — всё после ПЕРВОЙ строки `---`: до неё в документах
  `docs/legal/` живёт служебная преамбула (статус, что показать юристу),
  гостю она не предназначена. Правило записано и в самом документе.
- Рендер — минимальное подмножество Markdown без зависимостей, как страница
  веб-чата (`channels/web/page.py`, R-3/P-1): заголовки, списки, `**жирный**`,
  цитаты, `---`.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from hospitality.shared.config import Settings, get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

router = APIRouter(prefix="/legal", tags=["legal"])

# Путь роута — единственное место истины: из него собирается ссылка в тексте
# согласия (`privacy_policy_url`), им же объявлен маршрут ниже.
PRIVACY_POLICY_PATH = "/legal/privacy"

# Код каталога ошибок (docs/runbooks/errors.md, R-8): документ политики не
# найден или пуст. Блокер, а не косметика: ссылка в согласии ведёт в ошибку.
ERR_PRIVACY_POLICY_UNAVAILABLE = "ERR-PLATFORM-008"

_POLICY_RELATIVE_PATH = Path("docs") / "legal" / "privacy-policy.md"
# Разделитель служебной преамбулы и публикуемой части (см. докстринг модуля).
_PUBLIC_PART_MARKER = "\n---\n"

_BOLD_PATTERN = re.compile(r"\*\*(.+?)\*\*")


def privacy_policy_url(settings: Settings | None = None) -> str:
    """Абсолютный URL политики для текста согласия (spec 0029 §2).

    Абсолютный, а не путь: текст согласия уходит в Telegram, где относительная
    ссылка бессмысленна. Хост — `PUBLIC_BASE_URL` (та же переменная, которой
    деплой регистрирует вебхук).
    """
    base = (settings or get_settings()).public_base_url.rstrip("/")
    return f"{base}{PRIVACY_POLICY_PATH}"


def _candidate_paths() -> tuple[Path, ...]:
    """Где искать документ политики: репозиторий разработчика, затем рабочая
    директория процесса.

    Два кандидата вместо одного — потому что в образе пакет установлен в
    site-packages (`pip install .`), а `docs/legal/` лежит рядом с рабочей
    директорией `/app` (COPY в Dockerfile): путь «от `__file__` вверх» там не
    работает, а относительный от CWD — работает. Локально и в тестах верно
    обратное направление, поэтому проверяются оба.
    """
    repo_root = Path(__file__).resolve().parents[3]
    return (repo_root / _POLICY_RELATIVE_PATH, Path.cwd() / _POLICY_RELATIVE_PATH)


@lru_cache
def load_privacy_policy_markdown() -> str:
    """Публикуемая часть политики (Markdown); кэш — файл не меняется без рестарта."""
    for candidate in _candidate_paths():
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
        candidates=[str(path) for path in _candidate_paths()],
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


def render_privacy_policy() -> str:
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

    for line in load_privacy_policy_markdown().splitlines():
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
    return _PAGE_TEMPLATE.format(body="\n".join(blocks))


_PAGE_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy policy · Политика конфиденциальности</title>
<style>
  body {{ font:16px/1.55 system-ui,sans-serif; color:#1b1c1f; background:#fff;
         max-width:44rem; margin:0 auto; padding:24px 16px 64px; }}
  h1 {{ font-size:22px; margin:24px 0 8px; }}
  h2 {{ font-size:19px; margin:28px 0 8px; }}
  h3 {{ font-size:17px; margin:20px 0 6px; }}
  p, li {{ margin:8px 0; }}
  li {{ margin-left:20px; }}
  blockquote {{ margin:8px 0; padding-left:12px; border-left:3px solid #d6d8dc; color:#555; }}
  hr {{ border:0; border-top:1px solid #e3e5e8; margin:28px 0; }}
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
