"""Тексты брони столика (spec 0064 §5) — ru, kk, en: надписи страницы и сообщения в чат.

Статические строки, модель их не порождает. Русские — spec 0064 §5 (показаны
основателю до кода, ответ диспетчера 03.11 — «как есть»); kk и en — черновик сессии,
показаны файлом диспетчеру до кода (PR #716) и записаны в docs/founder-review/;
kk сверяет носитель. «Забронирован» (kk «брондалды», en «booked») — только в
тексте «Подтвердить»: до ответа ресторана это запрос (§2.2, тест по всем текстам).
Подстановки `{n}`, `{room}`, `{time}` заполняет скрипт страницы.
"""

from __future__ import annotations

from datetime import date
from typing import Final

# ключ → (ru, kk, en)
PAGE_TEXTS: Final[dict[str, tuple[str, str, str]]] = {
    "title": ("Столик в ресторане", "Мейрамханадағы үстел", "Table at a restaurant"),
    "back_chat": ("Назад в чат", "Чатқа оралу", "Back to chat"),
    "room": ("№ {room}", "№ {room}", "Room {room}"),
    "my_bookings": ("Ваши брони", "Сіздің брондарыңыз", "Your bookings"),
    "field_restaurant": ("Ресторан", "Мейрамхана", "Restaurant"),
    "field_date": ("Дата", "Күні", "Date"),
    "field_time": ("Время", "Уақыты", "Time"),
    "today": ("Сегодня", "Бүгін", "Today"),
    "tomorrow": ("Завтра", "Ертең", "Tomorrow"),
    # Подписи дней формы — свои, не `Intl`: казахских месяцев в браузере может не быть
    # (headless Chrome рисовал «M11 5, Thu»). Через запятую: месяцы с января, дни с понедельника.
    "months": (
        "янв,фев,мар,апр,мая,июн,июл,авг,сен,окт,ноя,дек",
        "қаң,ақп,нау,сәу,мам,мау,шіл,там,қыр,қаз,қар,жел",
        "Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec",
    ),
    "weekdays": ("пн,вт,ср,чт,пт,сб,вс", "дс,сс,ср,бс,жм,сб,жс", "Mon,Tue,Wed,Thu,Fri,Sat,Sun"),
    "field_party": ("Гостей", "Қонақ саны", "Guests"),
    "field_name": (
        "На чьё имя (необязательно)",
        "Кімнің атына (міндетті емес)",
        "Name for the booking (optional)",
    ),
    "field_wishes": (
        "Пожелания (необязательно)",
        "Тілектер (міндетті емес)",
        "Requests (optional)",
    ),
    "wishes_placeholder": (
        "Детский стульчик, столик у окна…",
        "Балалар орындығы, терезе жанындағы үстел…",
        "High chair, table by the window…",
    ),
    "submit": ("Отправить запрос", "Сұраныс жіберу", "Send request"),
    "submit_note": (
        "Это запрос, а не бронь: ресторан проверит места и ответит в чат.",
        "Бұл бронь емес, сұраныс: мейрамхана бос орындарды тексеріп, чатқа жауап береді.",
        "This is a request, not a booking: the restaurant will check for free tables and "
        "reply in the chat.",
    ),
    "party_over": (
        "Компанию больше {n} гостей ресторан бронирует сам — напишите нам в чат.",
        "{n} қонақтан көп топты мейрамхана өзі брондайды — бізге чатқа жазыңыз.",
        "Parties of more than {n} book with the restaurant directly — write to us in the chat.",
    ),
    "party_value": ("гостей: {n}", "қонақ: {n}", "guests: {n}"),
    "stage_waiting": (
        "Ждём ответа ресторана",
        "Мейрамхананың жауабын күтеміз",
        "Waiting for the restaurant's reply",
    ),
    "stage_confirmed": ("Столик забронирован", "Үстел брондалды", "Table booked"),
    "stage_other_time": (
        "Ресторан предлагает {time}",
        "Мейрамхана {time} уақытын ұсынады",
        "The restaurant suggests {time}",
    ),
    "stage_no_seats": ("Мест нет", "Орын жоқ", "No tables available"),
    "stage_cancelled": ("Отменена", "Жойылды", "Cancelled"),
    "stage_no_answer": (
        "Ресторан не ответил вовремя. Позвоните в ресторан или напишите нам в чат — поможем.",
        "Мейрамхана уақытында жауап бермеді. Мейрамханаға қоңырау шалыңыз немесе бізге "
        "чатқа жазыңыз — көмектесеміз.",
        "The restaurant didn't reply in time. Call the restaurant or write to us in the "
        "chat — we'll help.",
    ),
    "cancelled_after_confirm": (
        "Бронь отменена — мы передали это ресторану",
        "Бронь жойылды — мейрамханаға хабарладық",
        "Booking cancelled — we've let the restaurant know",
    ),
    "request_at": ("Запросить на {time}", "{time} уақытына сұрау", "Request {time}"),
    "cancel_request": ("Отменить запрос", "Сұраныстан бас тарту", "Cancel request"),
    "cancel_booking": ("Отменить бронь", "Броньнан бас тарту", "Cancel booking"),
    "keep": ("Оставить", "Қалдыру", "Keep it"),
    "no_slots": (
        "На этот день время для запроса уже прошло — выберите другой день.",
        "Бұл күнге сұраныс уақыты өтіп кетті — басқа күнді таңдаңыз.",
        "It's too late to request a table for this day — please pick another day.",
    ),
    "err_limit": (
        "Слишком много запросов — дождитесь ответа ресторана.",
        "Сұраныс тым көп — мейрамхананың жауабын күтіңіз.",
        "Too many requests — please wait for the restaurant's reply.",
    ),
    "err_invalid": (
        "Это время уже недоступно — выберите другое.",
        "Бұл уақыт енді қолжетімсіз — басқасын таңдаңыз.",
        "This time is no longer available — please pick another.",
    ),
    "err_cancel": (
        "Отменить уже нельзя — статус обновлён.",
        "Енді бас тарту мүмкін емес — күйі жаңартылды.",
        "It can no longer be cancelled — the status has been updated.",
    ),
    "err_network": (
        "Не получилось — проверьте интернет и попробуйте ещё раз.",
        "Болмады — интернетті тексеріп, қайталап көріңіз.",
        "Something went wrong — check your connection and try again.",
    ),
    "sent": ("Запрос отправлен", "Сұраныс жіберілді", "Request sent"),
}

# Сообщения в чат по шагам (§5): ключ шага → (ru, kk, en).
MESSAGES: Final[dict[str, tuple[str, str, str]]] = {
    "requested": (
        "Запрос отправлен в ресторан «{restaurant}»: {date}, {time}, гостей — {n}. Это ещё не "
        "бронь: ресторан проверит места и ответит здесь.",
        "Сұраныс «{restaurant}» мейрамханасына жіберілді: {date}, {time}, қонақ саны — {n}. "
        "Бұл әлі бронь емес: мейрамхана бос орындарды тексеріп, осы жерде жауап береді.",
        'Request sent to the restaurant "{restaurant}": {date}, {time}, guests: {n}. This is '
        "not a booking yet — the restaurant will check for free tables and reply here.",
    ),
    "confirmed": (
        "Столик забронирован: ресторан «{restaurant}», {date}, {time}, гостей — {n}. Ждём вас!",
        "Үстел брондалды: «{restaurant}» мейрамханасы, {date}, {time}, қонақ саны — {n}. "
        "Сізді күтеміз!",
        'Your table is booked: restaurant "{restaurant}", {date}, {time}, guests: {n}. We look '
        "forward to seeing you!",
    ),
    "other_time": (
        "В ресторане «{restaurant}» на {time} мест нет — ресторан предлагает {new_time}, "
        "{date}. Подходит — откройте «🍽 Забронировать столик» вверху чата и отправьте запрос "
        "на {new_time} одной кнопкой.",
        "«{restaurant}» мейрамханасында {time} уақытына орын жоқ — мейрамхана {date}, "
        "{new_time} уақытын ұсынады. Қолайлы болса, чаттың жоғарғы жағындағы «🍽 Үстел "
        "брондау» батырмасын ашып, {new_time} уақытына сұранысты бір батырмамен жіберіңіз.",
        'The restaurant "{restaurant}" has no tables at {time} — it suggests {new_time}, '
        '{date}. If that works, open "🍽 Book a table" at the top of the chat and send a '
        "request for {new_time} with one tap.",
    ),
    "no_seats": (
        "К сожалению, в ресторане «{restaurant}» нет мест на {date}, {time}. Попробуйте другое "
        "время или другой ресторан.",
        "Өкінішке қарай, «{restaurant}» мейрамханасында {date}, {time} уақытына орын жоқ. "
        "Басқа уақытты немесе басқа мейрамхананы таңдап көріңіз.",
        'Sorry, the restaurant "{restaurant}" has no tables on {date} at {time}. Please try '
        "another time or another restaurant.",
    ),
    # Возврат в работу (#998): ответ снят, тоном поправки обычной заявки; показан
    # основателю файлом до кода, запись в docs/founder-review/.
    "reopened": (
        "Простите, мы поторопились: ответ на запрос в ресторан «{restaurant}» на {date}, "
        "{time} отправлен по ошибке. Ресторан ещё проверяет места и ответит здесь.",
        "Кешіріңіз, қателесіп қалдық: «{restaurant}» мейрамханасына {date}, {time} уақытына "
        "жіберген сұранысыңызға жауап қате жіберілді. Мейрамхана бос орындарды әлі тексеріп "
        "жатыр, осы жерде жауап береді.",
        'Sorry, we were too quick: the reply to your request to the restaurant "{restaurant}" '
        "for {date}, {time} was sent by mistake. The restaurant is still checking for free "
        "tables and will reply here.",
    ),
    "no_answer": (
        "Ресторан «{restaurant}» не ответил на запрос на {date}, {time}. Позвоните в ресторан "
        "или напишите нам — поможем.",
        "«{restaurant}» мейрамханасы {date}, {time} уақытына сұранысқа жауап бермеді. "
        "Мейрамханаға қоңырау шалыңыз немесе бізге жазыңыз — көмектесеміз.",
        'The restaurant "{restaurant}" did not reply to your request for {date}, {time}. Call '
        "the restaurant or write to us — we'll help.",
    ),
}

# Месяцы для «5 ноября» / «5 қараша» / «5 November».
_MONTHS: Final[dict[str, tuple[str, ...]]] = {
    "ru": (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ),
    "kk": (
        "қаңтар",
        "ақпан",
        "наурыз",
        "сәуір",
        "мамыр",
        "маусым",
        "шілде",
        "тамыз",
        "қыркүйек",
        "қазан",
        "қараша",
        "желтоқсан",
    ),
    "en": (
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ),
}


def pick(texts: tuple[str, str, str], language: str) -> str:
    """Текст на языке гостя; язык не из трёх — английский."""
    ru, kk, en = texts
    return {"ru": ru, "kk": kk}.get(language, en)


def day_text(day: date, language: str) -> str:
    """«5 ноября» на языке гостя; язык не из трёх — английский."""
    months = _MONTHS.get(language, _MONTHS["en"])
    return f"{day.day} {months[day.month - 1]}"


def page_texts_json() -> dict[str, dict[str, str]]:
    """Все надписи страницы формой `{ключ: {ru, kk, en}}` — данные её скрипта."""
    return {key: {"ru": ru, "kk": kk, "en": en} for key, (ru, kk, en) in PAGE_TEXTS.items()}
