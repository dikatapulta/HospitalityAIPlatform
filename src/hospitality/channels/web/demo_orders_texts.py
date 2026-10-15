"""Надписи гостевой страницы «Заказать в номер» — демо к презентации (issue #438, дизайн — #440).

Статические строки на трёх языках, как остальные тексты веб-чата; модель их не
порождает. Показаны основателю в чате до кода (CLAUDE.md), строка — в
docs/FOUNDER_REVIEW_QUEUE.md; kk — черновик сессии, сверяет носитель. Формулировки
ресторана на шагах «Принят» и «Оплачено» — из утверждённой spec 0038 §8.1.
Подстановки `{room}`, `{n}`, `{total}`, `{method}`, `{min}` заполняет JS страницы.
"""

from __future__ import annotations

from typing import Final

from hospitality.channels.common.demo_orders import Localized

DEMO_TEXTS: Final[dict[str, Localized]] = {
    "title": Localized("Заказ в номер", "Нөмірге тапсырыс", "Room service"),
    "demo": Localized("Демо", "Демо", "Demo"),
    "demo_note": Localized(
        "заказ никуда не отправляется и ничего не списывается — так будет выглядеть заказ в номер.",
        "тапсырыс ешқайда жіберілмейді, ақша алынбайды — нөмірге тапсырыс осылай көрінеді.",
        "the order isn't sent anywhere and nothing is charged — this is how ordering to "
        "your room will look.",
    ),
    "back": Localized("Назад", "Артқа", "Back"),
    "tab_restaurant": Localized("Ресторан", "Мейрамхана", "Restaurant"),
    "tab_shop": Localized("Магазин", "Дүкен", "Shop"),
    "hero_restaurant": Localized(
        "Еда из ресторана — прямо в номер",
        "Мейрамхана тағамдары — тікелей нөміріңізге",
        "Restaurant food, straight to your room",
    ),
    "hero_shop": Localized(
        "Всё нужное — за пару минут",
        "Керектің бәрі — бірнеше минутта",
        "Everything you need, in minutes",
    ),
    "eta_restaurant": Localized("25–35 мин", "25–35 мин", "25–35 min"),
    "eta_shop": Localized("10–15 мин", "10–15 мин", "10–15 min"),
    "to_door": Localized("К двери номера", "Нөмір есігіне дейін", "To your door"),
    "search_restaurant": Localized("Найти блюдо", "Тағам іздеу", "Find a dish"),
    "search_shop": Localized("Найти товар", "Тауар іздеу", "Find an item"),
    "not_found": Localized(
        "Ничего не нашли — попробуйте другое слово",
        "Ештеңе табылмады — басқа сөзбен іздеп көріңіз",
        "Nothing found — try another word",
    ),
    "popular": Localized("Популярное", "Танымал", "Popular"),
    "badge_hit": Localized("Хит", "Хит", "Bestseller"),
    "badge_new": Localized("Новинка", "Жаңа", "New"),
    "badge_local": Localized("Местное", "Жергілікті", "Local"),
    "cart_restaurant": Localized("Корзина ресторана", "Мейрамхана себеті", "Restaurant cart"),
    "cart_shop": Localized("Корзина магазина", "Дүкен себеті", "Shop cart"),
    "clear": Localized("Очистить", "Тазалау", "Clear"),
    "add": Localized("Добавить", "Қосу", "Add"),
    "add_n": Localized("Добавить · {total}", "Қосу · {total}", "Add · {total}"),
    "remove": Localized("Убрать", "Алып тастау", "Remove"),
    "add_more": Localized("Добавить к заказу", "Тапсырысқа қосу", "Add to your order"),
    "deliver_to": Localized(
        "Принесём в номер {room}", "{room} нөмірге әкелеміз", "We'll bring it to room {room}"
    ),
    "by_code": Localized(
        "По вашему коду заселения", "Тіркелу кодыңыз бойынша", "Based on your check-in code"
    ),
    "asap": Localized("Как можно скорее", "Мүмкіндігінше тез", "As soon as possible"),
    "payment": Localized("Оплата при получении", "Алған кезде төлеу", "Pay on delivery"),
    "cash": Localized("Наличными", "Қолма-қол", "Cash"),
    "card": Localized("Картой", "Картамен", "Card"),
    "items": Localized("Состав", "Құрамы", "Items"),
    "notes": Localized("Пожелания", "Тілектер", "Notes"),
    "optional": Localized("Необязательно", "Міндетті емес", "Optional"),
    "total": Localized("Итого", "Барлығы", "Total"),
    "place_order": Localized(
        "Оформить заказ · {total}", "Тапсырыс беру · {total}", "Place order · {total}"
    ),
    "sending": Localized("Отправляем…", "Жіберіп жатырмыз…", "Sending…"),
    "order_n": Localized("Заказ #{n}", "№{n} тапсырыс", "Order #{n}"),
    "step_accepted": Localized("Принят", "Қабылданды", "Accepted"),
    "step_paid": Localized("Оплачено", "Төленді", "Paid"),
    "step_delivering": Localized("Несут к вам", "Сізге әкеле жатыр", "On its way"),
    "eta_status": Localized(
        "Будет примерно через {min} мин",
        "Шамамен {min} минуттан кейін жетеді",
        "Arrives in about {min} min",
    ),
    "text_accepted": Localized(
        "Сотрудник скоро подойдёт за оплатой: {total}, {method}.",
        "Қызметкер жақында төлемге келеді: {total}, {method}.",
        "A staff member will come shortly for payment: {total}, {method}.",
    ),
    "method_cash": Localized("наличными", "қолма-қол", "in cash"),
    "method_card": Localized("картой", "картамен", "by card"),
    "text_paid_restaurant": Localized(
        "Оплата получена, готовим. Принесём в номер {room}.",
        "Төлем алынды, дайындап жатырмыз. {room} нөмірге әкелеміз.",
        "Payment received — we're cooking. We'll bring it to room {room}.",
    ),
    "text_paid_shop": Localized(
        "Оплата получена, собираем заказ. Принесём в номер {room}.",
        "Төлем алынды, тапсырысты жинап жатырмыз. {room} нөмірге әкелеміз.",
        "Payment received — we're packing your order. We'll bring it to room {room}.",
    ),
    "text_delivering": Localized(
        "Сотрудник уже идёт в номер {room}.",
        "Қызметкер {room} нөмірге келе жатыр.",
        "A staff member is on the way to room {room}.",
    ),
    "demo_next": Localized("Демо: следующий шаг", "Демо: келесі қадам", "Demo: next step"),
    "order_more": Localized("Заказать ещё", "Тағы тапсырыс беру", "Order more"),
    "to_chat": Localized("Вернуться в чат", "Чатқа оралу", "Back to chat"),
}
