"""Тексты гостю о реквизитах для АВР (spec 0049 §6) — ru, kk, en.

Статические строки, модель их не порождает. Ru — из #493 и прототипа, en и kk —
черновик окна PR #500, показаны основателю 28.10 (§6). Надписи страницы, которых
в §6 нет (шапка, строки сводки, кнопки, «файл больше 10 МБ», «макросы»), —
черновик окна PR 4, утверждён основателем 29.10 («норм»; kk сверяет носитель). Подстановки `{name}`,
`{room}`, `{date}` заполняет скрипт страницы или `messages.py`.
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Final, Literal

from hospitality.channels.common.consent import normalize_language
from hospitality.shared.humanize import month_day_ru

GuestLanguage = Literal["en", "ru", "kk"]

# ключ → (ru, kk, en)
PAGE_TEXTS: Final[dict[str, tuple[str, str, str]]] = {
    "clip_label": (
        "Прислать документы для бухгалтерии",
        "Бухгалтерияға құжат жіберу",
        "Send documents to accounting",
    ),
    "clip_hint": (
        "Скрепка — реквизиты для закрывающих документов",
        "Қыстырғыш — жабу құжаттарына арналған деректемелер",
        "Paperclip — company details for closing documents",
    ),
    "topbar": (
        "Документы для бухгалтерии",
        "Бухгалтерияға арналған құжаттар",
        "Documents for accounting",
    ),
    "back_chat": ("Назад в чат", "Чатқа оралу", "Back to chat"),
    "title": (
        "Реквизиты для закрывающих документов",
        "Жабу құжаттарына арналған деректемелер",
        "Company details for closing documents",
    ),
    "lead": (
        "Бухгалтерия отеля подготовит АВР и счёт-фактуру к вашему выезду.",
        "Қонақүй бухгалтериясы АВР мен шот-фактураны сіз шығатын күнге дейін дайындайды.",
        "The hotel's accounting team will prepare the AVR (certificate of completion) and "
        "the invoice by your check-out.",
    ),
    "room": ("Номер {room}", "{room} нөмір", "Room {room}"),
    "room_sub": ("По вашему коду заселения", "Тіркелу кодыңыз бойынша", "From your check-in code"),
    "dates_sub": (
        "Даты проживания — из заселения",
        "Тұру күндері — тіркелуден",
        "Stay dates — from your check-in",
    ),
    "bill_to_label": ("На кого выставить", "Кімге рәсімдеу", "Bill to"),
    "bill_to_placeholder": (
        "Название компании или ИП",
        "Компания немесе ЖК атауы",
        "Company or sole proprietor name",
    ),
    "files_title": (
        "Прикрепите файл с реквизитами",
        "Деректемелері бар файлды тіркеңіз",
        "Attach a file with company details",
    ),
    "files_sub": (
        "PDF, Word или фото · до 5 файлов по 10 МБ",
        "PDF, Word немесе фото · 5 файлға дейін, әрқайсысы 10 МБ-қа дейін",
        "PDF, Word or photo · up to 5 files, 10 MB each",
    ),
    "files_pick": ("Выбрать файл", "Файлды таңдау", "Choose file"),
    "remove_file": ("Убрать файл", "Файлды алып тастау", "Remove file"),
    "note_label": (
        "Реквизиты или комментарий",
        "Деректемелер немесе түсініктеме",
        "Details or comment",
    ),
    "note_placeholder": (
        "Можно вписать БИН, адрес и банк — или написать «всё в файле»",
        "БСН, мекенжай мен банкті жазуға болады — немесе «бәрі файлда» деп жазыңыз",
        'You can type the BIN, address and bank — or just write "all in the file"',
    ),
    "note_hint": (
        "Необязательно, если прикрепили файл.",
        "Файл тіркесеңіз, міндетті емес.",
        "Optional if you attached a file.",
    ),
    "privacy": (
        "Увидят только бухгалтерия, ресепшен и руководство отеля.",
        "Тек қонақүй бухгалтериясы, ресепшені мен басшылығы көреді.",
        "Only the hotel's accounting, front desk and management will see this.",
    ),
    "submit": ("Отправить в бухгалтерию", "Бухгалтерияға жіберу", "Send to accounting"),
    "sending": ("Отправляем…", "Жіберіп жатырмыз…", "Sending…"),
    "sent_title": ("Отправлено в бухгалтерию", "Бухгалтерияға жіберілді", "Sent to accounting"),
    "sent_text": (
        "Закрывающие документы подготовят к вашему выезду {date}. Если что-то нужно "
        "уточнить, напишем сюда, в чат.",
        "Жабу құжаттары {date} шығуыңызға дейін дайын болады. Бірдеңе нақтылау керек "
        "болса, осы чатқа жазамыз.",
        "Your closing documents will be ready by check-out on {date}. If anything needs "
        "clarifying, we'll write to you here in the chat.",
    ),
    "sum_bill_to": ("На кого", "Кімге", "Bill to"),
    "sum_files": ("Файлы", "Файлдар", "Files"),
    "sum_text": ("Текст", "Мәтін", "Text"),
    "sum_room": ("Номер", "Нөмір", "Room"),
    "sum_none": ("нет", "жоқ", "none"),
    "send_more": ("Прислать ещё документ", "Тағы құжат жіберу", "Send another document"),
    "back_to_chat": ("Вернуться в чат", "Чатқа оралу", "Back to chat"),
    "err_format": (
        "Этот формат не подойдёт: {name}. Пришлите PDF, Word (.docx) или фото (JPG, PNG).",
        "Бұл формат жарамайды: {name}. PDF, Word (.docx) немесе фото (JPG, PNG) жіберіңіз.",
        "This format won't work: {name}. Please send a PDF, Word (.docx) or a photo (JPG, PNG).",
    ),
    "err_too_large": (
        "Файл {name} больше 10 МБ. Сожмите его или пришлите фото страницы.",
        "{name} файлы 10 МБ-тан үлкен. Оны қысыңыз немесе беттің фотосын жіберіңіз.",
        "{name} is larger than 10 MB. Please compress it or send a photo of the page.",
    ),
    "err_macros": (
        "В документе {name} есть макросы. Сохраните его как обычный Word (.docx) или PDF.",
        "{name} құжатында макростар бар. Оны кәдімгі Word (.docx) немесе PDF ретінде сақтаңыз.",
        "{name} contains macros. Please save it as a regular Word (.docx) or PDF.",
    ),
    "err_empty": (
        "Укажите, на кого выставить документы, или прикрепите файл.",
        "Құжаттарды кімге рәсімдеу керегін жазыңыз немесе файл тіркеңіз.",
        "Tell us who to bill, or attach a file.",
    ),
    "err_too_many_files": (
        "Можно прикрепить не больше 5 файлов.",
        "5 файлдан артық тіркеуге болмайды.",
        "You can attach up to 5 files.",
    ),
    "err_stay_limit": (
        "Вы уже отправили 5 запросов. Если нужно что-то поправить — напишите в чат.",
        "Сіз 5 сұрау жіберіп қойдыңыз. Бірдеңені түзету керек болса, чатқа жазыңыз.",
        "You've already sent 5 requests. If something needs fixing, write to us in the chat.",
    ),
    "err_failed": (
        "Не получилось отправить. Попробуйте ещё раз через минуту.",
        "Жіберу мүмкін болмады. Бір минуттан кейін қайталап көріңіз.",
        "Couldn't send. Please try again in a minute.",
    ),
    "err_disabled": (
        "Приём документов сейчас выключен.",
        "Құжаттарды қабылдау қазір өшірулі.",
        "Document upload is turned off right now.",
    ),
}

# Сообщения в чат (Р-2). Примечание бухгалтерии — как написано, без перевода.
RECEIVED: Final = (
    "📄 Реквизиты получены. Бухгалтерия подготовит закрывающие документы к выезду {date}. "
    "Вопросы — здесь, в чате.",
    "📄 Деректемелер алынды. Бухгалтерия жабу құжаттарын {date} шығуыңызға дейін дайындайды. "
    "Сұрақтарыңызды осы чатқа жазыңыз.",
    "📄 Company details received. Accounting will prepare your closing documents by check-out "
    "on {date}. Any questions — right here in the chat.",
)
READY: Final = (
    "📄 Бухгалтерия подготовила закрывающие документы. Заберите их на ресепшене при выезде.",
    "📄 Бухгалтерия жабу құжаттарын дайындады. Шығарда оларды ресепшеннен алыңыз.",
    "📄 Accounting has prepared your closing documents. Please collect them at the front desk "
    "when you check out.",
)
READY_NOTE: Final = (
    "Примечание бухгалтерии: {note}",
    "Бухгалтерия ескертпесі: {note}",
    "Note from accounting: {note}",
)
RETURNED: Final = (
    "📄 Бухгалтерия вернула запрос{note}. Пришлите документы ещё раз — скрепка внизу.",
    "📄 Бухгалтерия сұрауды қайтарды{note}. Құжаттарды қайта жіберіңіз — төмендегі қыстырғыш.",
    "📄 Accounting returned your request{note}. Please send the documents again — paperclip below.",
)

_MONTHS_KK: Final = (
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
)
_MONTHS_EN: Final = (
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
)


def guest_language(raw: str | None) -> GuestLanguage:
    """Язык браузера → en, ru или kk (Р-9); незнакомый — английский."""
    language = normalize_language(raw)
    if language == "ru":
        return "ru"
    if language == "kk":
        return "kk"
    return "en"


def pick(texts: tuple[str, str, str], language: str) -> str:
    ru, kk, en = texts
    return {"ru": ru, "kk": kk}.get(language, en)


def day_label(moment: datetime, zone: tzinfo, language: str) -> str:
    """Дата в поясе отеля словами: «28 октября», «28 қазан», «28 October» (§6)."""
    day = moment.astimezone(zone).date()
    if language == "ru":
        return month_day_ru(day)
    months = _MONTHS_KK if language == "kk" else _MONTHS_EN
    return f"{day.day} {months[day.month - 1]}"


def page_texts_json() -> dict[str, dict[str, str]]:
    """Все надписи формой `{ключ: {ru, kk, en}}` — данные скрипта страницы."""
    return {key: {"ru": ru, "kk": kk, "en": en} for key, (ru, kk, en) in PAGE_TEXTS.items()}
