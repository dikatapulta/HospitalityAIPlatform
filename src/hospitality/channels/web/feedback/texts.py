"""Надписи виджета оценки (spec 0076 §8, тексты — «Да, как у окна» основателя 10.11).

Единственный их владелец; страница чата вклеивает их в `page.CHAT_TEXTS`, язык — тот же,
что у остальных надписей чата. Кнопка поля и сбой — прежние `send` и `not_delivered` чата.
"""

from __future__ import annotations

from typing import Final

FEEDBACK_TEXTS: Final[dict[str, dict[str, str]]] = {
    "fb_ask": {"kk": "Бәрі қалай өтті?", "ru": "Как всё прошло?", "en": "How did it go?"},
    "fb_up": {"kk": "Жақсы", "ru": "Хорошо", "en": "Good"},
    "fb_down": {"kk": "Нашар", "ru": "Плохо", "en": "Bad"},
    "fb_thanks": {
        "kk": "Бағаңызға рахмет!",
        "ru": "Спасибо за оценку!",
        "en": "Thanks for the feedback!",
    },
    # Только когда карточка по оценке есть (`passed_to_staff`): иначе «передадим» — неправда.
    "fb_sorry": {
        "kk": "Өкінішті. Не дұрыс болмағанын жазыңыз — қызметкерлерге жеткіземіз.",
        "ru": "Жаль, что так вышло. Напишите, что было не так, — передадим сотрудникам.",
        "en": "Sorry about that. Tell us what went wrong — we'll pass it on to the staff.",
    },
    "fb_hint": {
        "kk": "Мысалы: басқа нәрсе әкелінді",
        "ru": "Например: принесли не то",
        "en": "For example: they brought the wrong thing",
    },
    # Только когда комментарий дошёл до карточки; иначе — «Спасибо за оценку!».
    "fb_sent": {
        "kk": "Рахмет, жеткіздік.",
        "ru": "Спасибо, передали.",
        "en": "Thanks, we've passed it on.",
    },
}
