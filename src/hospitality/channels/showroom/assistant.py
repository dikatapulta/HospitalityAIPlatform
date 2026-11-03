"""Блок помощника на странице 3D-витрины (spec 0069 §2.3, §5): поле, подсказка, реплика.

Вклеивается в страницу посетителя и в кабинет одной разметкой `HTML` + `CSS` + `JS`
(канон страниц без сборки, spec 0027 §3.4). Тексты — ru из spec 0069 §5, en и kk —
перевод окна; показаны диспетчеру до кода (ответ 04.11 — «как есть», запись в
docs/founder-review/). Команды ответа скрипт отдаёт событием `showroom:command`:
камерой правит просмотрщик, карточку `draft_request` открывает форма запроса.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from hospitality.ai.showroom.navigate import RESTING_TEXTS

_ASSETS: Final = Path(__file__).resolve().parent / "assets"
CSS: Final = (_ASSETS / "assistant.css").read_text(encoding="utf-8")
JS: Final = (_ASSETS / "assistant.js").read_text(encoding="utf-8")

# ключ → (ru, kk, en); «resting» — из ai/showroom: тот же текст, что у ответа `fallback`.
TEXTS: Final[dict[str, tuple[str, str, str]]] = {
    "placeholder_group": (
        "Например: высокий этаж с видом на горы, двенадцать номеров рядом",
        "Мысалы: таулар көрінетін жоғарғы қабат, қатар он екі бөлме",
        "For example: a high floor with a mountain view, twelve rooms next to each other",
    ),
    "placeholder_booking": (
        "Например: с 12 по 15 марта, двое, вид на горы",
        "Мысалы: 12–15 наурыз, екі адам, тау көрінісі",
        "For example: 12 to 15 March, two people, mountain view",
    ),
    "button": ("Показать", "Көрсету", "Show"),
    "hint": (
        "Не пишите сюда имена и контакты — помощник только показывает здание и готовит запрос.",
        "Мұнда аты-жөніңізді және байланыс деректерін жазбаңыз — көмекші тек ғимаратты "
        "көрсетіп, сұранысты дайындайды.",
        "Don't write names or contacts here — the assistant only shows the building and "
        "prepares a request.",
    ),
    "resting": (RESTING_TEXTS["ru"], RESTING_TEXTS["kk"], RESTING_TEXTS["en"]),
}

HTML: Final = """<form class="assistant" id="showroom-assistant" autocomplete="off">
<div class="assistant-row">
<input name="text" type="text" maxlength="300" enterkeyhint="send" aria-label="Assistant">
<button type="submit"></button>
</div>
<p class="assistant-hint"></p>
<p class="assistant-reply" role="status" aria-live="polite" hidden></p>
</form>
<script id="showroom-assistant-texts" type="application/json">__TEXTS__</script>"""


def texts_json() -> str:
    """Тексты блока JSON'ом для `<script>`: `<` экранирован — данные живут внутри тега."""
    by_language = {key: {"ru": ru, "kk": kk, "en": en} for key, (ru, kk, en) in TEXTS.items()}
    return json.dumps(by_language, ensure_ascii=False).replace("<", "\\u003c")


def render_block() -> str:
    """Разметка блока с текстами; CSS и JS страница вклеивает своими тегами."""
    return HTML.replace("__TEXTS__", texts_json())
