"""Надписи форм запроса 3D-витрины на трёх языках (spec 0069 §5; PR (в), #805).

Русские — дословно spec 0069 §5 (показаны основателю до кода, ответ диспетчера
03–04.11, founder-review `2026-11-03-pr801-showroom-texts.md`); английские и
казахские — перевод окна, показан диспетчеру файлом `ask/3-805-*` (не блокирует).
en первым: агенты из-за рубежа (§2.2). Скрипт формы берёт их JSON'ом.
"""

from __future__ import annotations

from typing import Final

from hospitality.channels.showroom.consent import consent_html

LANGUAGES: Final = ("en", "ru", "kk")

_TEXTS: Final[dict[str, dict[str, str]]] = {
    "group_title": {
        "en": "Request to the sales team",
        "ru": "Запрос в отдел продаж",
        "kk": "Сату бөліміне сұрау",
    },
    "booking_title": {
        "en": "Booking request",
        "ru": "Запрос на бронирование",
        "kk": "Брондауға сұрау",
    },
    "company": {"en": "Company", "ru": "Компания", "kk": "Компания"},
    "contact_name": {"en": "Contact person", "ru": "Контактное лицо", "kk": "Байланыс тұлғасы"},
    "guest_name": {"en": "Name", "ru": "Имя", "kk": "Аты-жөні"},
    "phone": {"en": "Phone", "ru": "Телефон", "kk": "Телефон"},
    "email": {"en": "Email", "ru": "Почта", "kk": "Пошта"},
    "guests": {"en": "Guests", "ru": "Гостей", "kk": "Қонақтар саны"},
    "check_in": {"en": "Check-in", "ru": "Заезд", "kk": "Келу күні"},
    "check_out": {"en": "Check-out", "ru": "Выезд", "kk": "Кету күні"},
    "rooms": {"en": "Selected rooms", "ru": "Выбранные номера", "kk": "Таңдалған нөмірлер"},
    "rooms_empty": {
        "en": "Choose rooms on the model — or leave it to the sales team.",
        "ru": "Выберите номера на модели — или оставьте подбор отделу продаж.",
        "kk": "Нөмірлерді модельден таңдаңыз — немесе таңдауды сату бөліміне қалдырыңыз.",
    },
    "remove_room": {"en": "Remove", "ru": "Убрать", "kk": "Алып тастау"},
    "wish": {
        "en": "Preferred room or view",
        "ru": "Желаемый номер или вид",
        "kk": "Қалаған нөмір немесе көрініс",
    },
    "wish_any": {"en": "any", "ru": "любой", "kk": "кез келген"},
    "wishes": {"en": "Wishes", "ru": "Пожелания", "kk": "Тілектер"},
    "wishes_placeholder": {
        "en": "Rooms on nearby floors, breakfast, transfer…",
        "ru": "Этажи рядом, завтраки, трансфер…",
        "kk": "Қатар қабаттар, таңғы ас, трансфер…",
    },
    "send": {"en": "Send request", "ru": "Отправить запрос", "kk": "Сұрауды жіберу"},
    "group_note": {
        "en": (
            "This is a request, not a booking: the sales team will contact you "
            "to confirm availability and terms."
        ),
        "ru": ("Это запрос, а не бронь: отдел продаж свяжется с вами, уточнит наличие и условия."),
        "kk": (
            "Бұл брондау емес, сұрау: сату бөлімі сізбен байланысып, бос нөмірлер мен "
            "шарттарды нақтылайды."
        ),
    },
    "booking_note": {
        "en": (
            "This is a request, not a booking: the reservations team will check "
            "availability and reply."
        ),
        "ru": "Это запрос, а не бронь: отдел бронирования проверит наличие и ответит вам.",
        "kk": (
            "Бұл брондау емес, сұрау: брондау бөлімі бос нөмірлерді тексеріп, сізге жауап береді."
        ),
    },
    "group_sent": {
        "en": "Request sent. The hotel's sales team will reply by phone or email.",
        "ru": "Запрос отправлен. Отдел продаж отеля ответит вам по телефону или почте.",
        "kk": "Сұрау жіберілді. Қонақүйдің сату бөлімі сізге телефон немесе пошта арқылы "
        "жауап береді.",
    },
    "booking_sent": {
        "en": "Request sent. The hotel will reply by phone or email.",
        "ru": "Запрос отправлен. Отель ответит вам по телефону или почте.",
        "kk": "Сұрау жіберілді. Қонақүй сізге телефон немесе пошта арқылы жауап береді.",
    },
    "draft_title": {
        "en": "Check your request",
        "ru": "Проверьте запрос",
        "kk": "Сұрауды тексеріңіз",
    },
    "draft_note": {
        "en": (
            "The assistant filled this in from what you said. Check it and add your "
            "contacts — nothing is sent until you press the button."
        ),
        "ru": (
            "Помощник заполнил это по вашим словам. Проверьте, добавьте контакты — "
            "без вашей кнопки ничего не отправится."
        ),
        "kk": (
            "Көмекші мұны сіздің сөздеріңіз бойынша толтырды. Тексеріп, байланыс "
            "деректерін қосыңыз — сіздің батырмаңызсыз ештеңе жіберілмейді."
        ),
    },
    "edit": {"en": "Edit", "ru": "Изменить", "kk": "Өзгерту"},
    # Вид прототипа (#819): крестик формы, «Даты» в карточке отправленного, возврат к модели.
    "close": {"en": "Close", "ru": "Закрыть", "kk": "Жабу"},
    "dates": {"en": "Dates", "ru": "Даты", "kk": "Күндер"},
    "back": {"en": "Back to the model", "ru": "Назад к модели", "kk": "Модельге оралу"},
    "need_contact": {
        "en": "Enter a phone or email — the hotel will reply to one of them.",
        "ru": "Укажите телефон или почту — отель ответит по одному из них.",
        "kk": "Телефонды немесе поштаны көрсетіңіз — қонақүй солардың біріне жауап береді.",
    },
    "invalid": {
        "en": "Check the dates, number of guests and rooms — the request did not pass the check.",
        "ru": "Проверьте даты, число гостей и номера — запрос не прошёл проверку.",
        "kk": "Күндерді, қонақтар санын және нөмірлерді тексеріңіз — сұрау тексеруден өтпеді.",
    },
    "rate_limited": {
        "en": "Too many requests from your address — try later or contact the hotel directly.",
        "ru": "Слишком много запросов с вашего адреса — попробуйте позже или напишите отелю "
        "напрямую.",
        "kk": "Сіздің мекенжайыңыздан сұраулар тым көп — кейінірек қайталаңыз немесе "
        "қонақүйге тікелей жазыңыз.",
    },
    "network": {
        "en": "Could not send — check your connection and try again.",
        "ru": "Не получилось отправить — проверьте интернет и попробуйте ещё раз.",
        "kk": "Жіберу мүмкін болмады — интернетті тексеріп, қайталап көріңіз.",
    },
}


def form_texts(language: str) -> dict[str, str]:
    """Надписи на языке страницы; чужой язык — английский. Согласие — HTML со ссылкой."""
    language = language if language in LANGUAGES else "en"
    texts = {key: variants[language] for key, variants in _TEXTS.items()}
    texts["consent_html"] = consent_html(language)
    return texts
