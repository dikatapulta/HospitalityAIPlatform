"""Что видит гость веб-чата, когда чат не отвечает (#991): куда идти — на ресепшен.

Три места. Строка под недоставленным сообщением и ошибка входа (код, ссылка с листка) —
когда сервер ответил 5xx или пропала сеть: страница уже открыта, цифру ресепшена она взяла
при загрузке. Страница вместо JSON 500 на гостевых адресах — собрана при импорте, без базы:
та в этот момент чаще всего и лежит. Отдаёт её обработчик необработанной ошибки
(`shared/errors.py`), подключает — composition root (`app.py`).

Слова — текста ЧП-перехвата (`ai/urgency.py`: «позвоните на ресепшен … с телефона в номере
или подойдите к стойке»), вместо «прямо сейчас» — «если срочно»: недоставленное — не всегда ЧП.
"""

from __future__ import annotations

import html
from typing import Final

from hospitality.channels.web.styles import THEME_COLOR_META, tight_html

LANGUAGES: Final = ("kk", "ru", "en")

# С `{dial}` — когда отель задал, что набрать с телефона в номере
# (`TenantConfig.reception_room_dial`); без настройки — фраза без цифры.
_CALL_RECEPTION: Final[dict[str, str]] = {
    "kk": "Шұғыл болса — бөлмедегі телефоннан ресепшнге қоңырау шалыңыз немесе ресепшнге барыңыз.",
    "ru": "Если срочно — позвоните на ресепшен с телефона в номере или подойдите к стойке.",
    "en": "If it's urgent, call reception from your room phone or go to the front desk.",
}
_CALL_RECEPTION_DIAL: Final[dict[str, str]] = {
    "kk": "Шұғыл болса — ресепшнге қоңырау шалыңыз (бөлмедегі телефоннан {dial}) немесе "
    "ресепшнге барыңыз.",
    "ru": "Если срочно — позвоните на ресепшен ({dial} с телефона в номере) или подойдите "
    "к стойке.",
    "en": "If it's urgent, call reception (dial {dial} from your room phone) or go to the front "
    "desk.",
}

_UNAVAILABLE_LEAD: Final = (
    "Чат қазір жұмыс істемей тұр, кейінірек қайталап көріңіз. Шұғыл болса — бөлмедегі "
    "телефоннан ресепшнге қоңырау шалыңыз немесе ресепшнге барыңыз.\n"
    "Чат сейчас не работает, попробуйте позже. Если срочно — позвоните на ресепшен "
    "с телефона в номере или подойдите к стойке.\n"
    "The chat isn't working right now, please try again later. If it's urgent, call "
    "reception from your room phone or go to the front desk."
)

# Карточка экрана входа без шапки: названия отеля нет — его негде прочитать.
_UNAVAILABLE_TEMPLATE: Final = tight_html(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
__THEME__
<title>Hotel guest chat</title>
<style>
__CSS__
</style>
</head>
<body>
<div class="wall" aria-hidden="true"></div>
<div class="app"><div class="scroller"><section class="gate"><div class="card">
<h1>Қонақүй чаты
Чат с отелем
Hotel guest chat</h1>
<p class="lead" role="alert">__LEAD__</p>
</div></section></div></div>
</body>
</html>
"""
)


def call_reception(dial: str | None) -> dict[str, str]:
    """«Если срочно — позвоните на ресепшен …» по языкам; с цифрой, если отель её задал."""
    if not dial:
        return dict(_CALL_RECEPTION)
    return {code: text.format(dial=dial) for code, text in _CALL_RECEPTION_DIAL.items()}


def gate_down(gate_error: str, dial: str | None) -> str:
    """Ошибка входа при 5xx и обрыве сети: язык гостя до входа неизвестен — все три."""
    call = call_reception(dial)
    return "\n".join([gate_error, *(call[code] for code in LANGUAGES)])


def unavailable_page(css: str) -> str:
    """Страница «чат не работает» в стилях чата (`css` — сжатые стили страницы чата)."""
    return (
        _UNAVAILABLE_TEMPLATE.replace("__THEME__", THEME_COLOR_META)
        .replace("__CSS__", css)
        .replace("__LEAD__", html.escape(_UNAVAILABLE_LEAD))
    )
