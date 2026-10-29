"""Надписи страницы «Заказать в номер» (spec 0038 §5) — ru, kk, en.

Статические строки, модель их не порождает. Русские — с одобренного прототипа
(эталон ревью spec 0038), kk и en показаны основателю до кода и утверждены им
28.10 (kk причины «не подтвердили» — его правка). Ключ с хвостом
`_restaurant` / `_shop` — вариант каталога. Подстановки `{n}`, `{room}`,
`{total}`, `{percent}` заполняет скрипт страницы.
"""

from __future__ import annotations

from typing import Final

# ключ → (ru, kk, en)
PAGE_TEXTS: Final[dict[str, tuple[str, str, str]]] = {
    "title": ("Заказ в номер", "Нөмірге тапсырыс", "Room service"),
    "back_chat": ("Назад в чат", "Чатқа оралу", "Back to chat"),
    "back": ("Назад", "Артқа", "Back"),
    "room": ("№ {room}", "№ {room}", "Room {room}"),
    "tab_restaurant": ("Ресторан", "Мейрамхана", "Restaurant"),
    "tab_shop": ("Магазин", "Дүкен", "Shop"),
    "hero_restaurant": ("Еда в номер", "Нөмірге тамақ", "Food to your room"),
    "hero_shop": ("Магазин отеля", "Қонақүй дүкені", "Hotel shop"),
    # Заказ принят только после оплаты у двери — слово основателя 29.10 (ревью PR #520, Н-11).
    "fact_pay": ("Оплата у двери", "Есік алдында төлеу", "Pay at your door"),
    "fact_after_restaurant": (
        "Заказ принимается после оплаты",
        "Тапсырыс төлемнен кейін қабылданады",
        "Order accepted after payment",
    ),
    "fact_after_shop": (
        "Заказ принимается после оплаты",
        "Тапсырыс төлемнен кейін қабылданады",
        "Order accepted after payment",
    ),
    "closed_restaurant": (
        "Сейчас заказы в номер не принимаются",
        "Қазір нөмірге тапсырыс қабылданбайды",
        "Room orders are not being accepted right now",
    ),
    "closed_shop": (
        "Магазин сейчас не принимает заказы",
        "Дүкен қазір тапсырыс қабылдамайды",
        "The shop isn't taking orders right now",
    ),
    "closed_note": (
        "Меню можно посмотреть. Чтобы заказать, обратитесь на ресепшен.",
        "Мәзірді қарауға болады. Тапсырыс беру үшін ресепшенге хабарласыңыз.",
        "You can browse the menu. To order, please contact the reception.",
    ),
    "closed_bar": (
        "Заказы сейчас не принимаются",
        "Қазір тапсырыс қабылданбайды",
        "Orders are not being accepted right now",
    ),
    "empty_catalog": (
        "Здесь пока пусто",
        "Әзірге бос",
        "Nothing here yet",
    ),
    "sold_out": ("Закончилось", "Таусылды", "Sold out"),
    "sold_out_line": (
        "Закончилось — уберите из корзины",
        "Таусылды — себеттен алып тастаңыз",
        "Sold out — remove it from the cart",
    ),
    "add": ("Добавить", "Қосу", "Add"),
    "update": ("Обновить", "Жаңарту", "Update"),
    "more": ("Ещё одну", "Тағы біреу", "One more"),
    "less": ("Убрать одну", "Біреуін алып тастау", "One less"),
    "cart": ("Корзина", "Себет", "Cart"),
    "clear": ("Очистить", "Тазалау", "Clear"),
    "next": ("Далее", "Әрі қарай", "Next"),
    "subtotal_restaurant": ("Блюда", "Тағамдар", "Dishes"),
    "subtotal_shop": ("Товары", "Тауарлар", "Items"),
    "charge": ("Сервисный сбор {percent}%", "Қызмет ақысы {percent}%", "Service charge {percent}%"),
    "total": ("Итого", "Барлығы", "Total"),
    "charge_hint_restaurant": (
        "Сервисный сбор ресторана — {percent}% от суммы блюд.",
        "Мейрамхананың қызмет ақысы — тағамдар сомасының {percent}%.",
        "The restaurant's service charge is {percent}% of the dishes total.",
    ),
    "charge_hint_shop": (
        "Сервисный сбор магазина — {percent}% от суммы товаров.",
        "Дүкеннің қызмет ақысы — тауарлар сомасының {percent}%.",
        "The shop's service charge is {percent}% of the items total.",
    ),
    "remove_sold_out_restaurant": (
        "Уберите блюда, которые закончились",
        "Таусылған тағамдарды алып тастаңыз",
        "Remove the dishes that are sold out",
    ),
    "remove_sold_out_shop": (
        "Уберите товары, которые закончились",
        "Таусылған тауарларды алып тастаңыз",
        "Remove the items that are sold out",
    ),
    "checkout": ("Ваш заказ", "Сіздің тапсырысыңыз", "Your order"),
    "checkout_sub": ("Проверьте и оформите", "Тексеріп, рәсімдеңіз", "Check and place it"),
    "deliver_room": ("Номер {room}", "{room} нөмір", "Room {room}"),
    "by_code": ("По вашему коду заселения", "Тіркелу кодыңыз бойынша", "By your check-in code"),
    "asap": ("Как можно скорее", "Мүмкіндігінше тез", "As soon as possible"),
    "pay_title": ("Оплата у двери", "Есік алдында төлеу", "Pay at your door"),
    "pay_sub": (
        "Сотрудник подойдёт за оплатой — наличными или картой",
        "Қызметкер төлемге келеді — қолма-қол немесе картамен",
        "A staff member will come for payment — cash or card",
    ),
    "items": ("Состав", "Құрамы", "Items"),
    "comment": ("Пожелания", "Тілектер", "Notes"),
    "comment_placeholder_restaurant": (
        "Например: без лука, приборы на двоих",
        "Мысалы: пиязсыз, екі адамға құрал-сайман",
        "E.g. no onions, cutlery for two",
    ),
    "comment_placeholder_shop": (
        "Например: зарядка для iPhone",
        "Мысалы: iPhone-ға арналған зарядтағыш",
        "E.g. a charger for iPhone",
    ),
    "comment_hint": (
        "Необязательно. До 200 символов.",
        "Міндетті емес. 200 таңбаға дейін.",
        "Optional. Up to 200 characters.",
    ),
    "place": ("Оформить заказ", "Тапсырыс беру", "Place order"),
    "sending": ("Отправляем…", "Жіберіп жатырмыз…", "Sending…"),
    "order_n": ("Заказ #{n}", "№{n} тапсырыс", "Order #{n}"),
    "stage_sent": ("Отправлен", "Жіберілді", "Sent"),
    "stage_coming": ("Идём за оплатой", "Төлемге келе жатырмыз", "Coming for payment"),
    "stage_paid": ("Принят", "Қабылданды", "Accepted"),
    "stage_delivered": ("Доставлен", "Жеткізілді", "Delivered"),
    "stage_cancelled": ("Отменён", "Болдырылмады", "Cancelled"),
    "step_sent_restaurant": (
        "Заказ ушёл в ресторан",
        "Тапсырыс мейрамханаға кетті",
        "Sent to the restaurant",
    ),
    "step_sent_shop": ("Заказ ушёл в магазин", "Тапсырыс дүкенге кетті", "Sent to the shop"),
    "step_coming": (
        "Сотрудник идёт к вам",
        "Қызметкер сізге келе жатыр",
        "A staff member is on the way",
    ),
    "step_paid_restaurant": ("Оплачен, готовим", "Төленді, дайындап жатырмыз", "Paid — cooking"),
    "step_paid_shop": (
        "Оплачен, собираем заказ",
        "Төленді, тапсырысты жинап жатырмыз",
        "Paid — packing your order",
    ),
    "step_delivered": ("Принесли в номер", "Нөмірге әкелдік", "Brought to your room"),
    "title_sent_restaurant": (
        "Заказ отправлен в ресторан",
        "Тапсырыс мейрамханаға жіберілді",
        "Order sent to the restaurant",
    ),
    "title_sent_shop": (
        "Заказ отправлен в магазин",
        "Тапсырыс дүкенге жіберілді",
        "Order sent to the shop",
    ),
    "title_coming": (
        "Сотрудник идёт к вам за оплатой",
        "Қызметкер сізге төлемге келе жатыр",
        "A staff member is coming for payment",
    ),
    "title_paid_restaurant": (
        "Заказ принят, готовим",
        "Тапсырыс қабылданды, дайындап жатырмыз",
        "Order accepted — we're cooking",
    ),
    "title_paid_shop": ("Заказ принят", "Тапсырыс қабылданды", "Order accepted"),
    "title_delivered": ("Заказ доставлен", "Тапсырыс жеткізілді", "Order delivered"),
    "title_cancelled": ("Заказ отменён", "Тапсырыс болдырылмады", "Order cancelled"),
    "text_sent_restaurant": (
        "Сотрудник придёт к вам за оплатой — наличными или картой. "
        "Заказ примем и начнём готовить после оплаты.",
        "Қызметкер сізге төлемге келеді — қолма-қол немесе картамен. "
        "Тапсырысты төлемнен кейін қабылдап, дайындай бастаймыз.",
        "A staff member will come to you for payment — cash or card. "
        "We accept the order and start cooking after payment.",
    ),
    "text_sent_shop": (
        "Сотрудник придёт к вам — оплата наличными или картой, заказ отдадим после оплаты.",
        "Қызметкер сізге келеді — қолма-қол немесе картамен төлейсіз, "
        "тапсырысты төлемнен кейін береміз.",
        "A staff member will come to you — pay by cash or card, "
        "and we'll hand over the order after payment.",
    ),
    "text_coming": (
        "К оплате {total} — наличными или картой.",
        "Төлемге {total} — қолма-қол немесе картамен.",
        "Amount due: {total} — cash or card.",
    ),
    "text_paid_restaurant": (
        "Принесём в номер {room}.",
        "{room} нөмірге әкелеміз.",
        "We'll bring it to room {room}.",
    ),
    "text_paid_shop": ("Спасибо!", "Рақмет!", "Thank you!"),
    "text_delivered_restaurant": ("Приятного аппетита!", "Ас болсын!", "Enjoy your meal!"),
    "text_delivered_shop": ("Хорошего отдыха!", "Демалысыңыз жақсы өтсін!", "Enjoy your stay!"),
    "text_cancelled_guest": (
        "Вы отменили заказ. Ничего платить не нужно.",
        "Сіз тапсырыстан бас тарттыңыз. Ештеңе төлеудің қажеті жоқ.",
        "You cancelled the order. Nothing to pay.",
    ),
    "text_cancelled_staff": (
        "Причина: {reason}. Если это ошибка — напишите нам в чат.",
        "Себебі: {reason}. Қате болса — бізге чатқа жазыңыз.",
        "Reason: {reason}. If this is a mistake, write to us in the chat.",
    ),
    "text_refund": (
        "Деньги за заказ вам вернут — сотрудник свяжется с вами.",
        "Төлеген ақшаңыз қайтарылады — қызметкер сізбен хабарласады.",
        "Your payment will be refunded — a staff member will contact you.",
    ),
    "cancel": ("Отменить заказ", "Тапсырысты болдырмау", "Cancel order"),
    "cancel_ask": (
        "Отменить заказ #{n}?",
        "№{n} тапсырысты болдырмау керек пе?",
        "Cancel order #{n}?",
    ),
    "cancel_free_restaurant": (
        "Ресторан ещё не принял заказ — отмена бесплатная.",
        "Мейрамхана тапсырысты әлі қабылдаған жоқ — болдырмау тегін.",
        "The restaurant hasn't accepted the order yet — cancelling is free.",
    ),
    "cancel_free_shop": (
        "Магазин ещё не принял заказ — отмена бесплатная.",
        "Дүкен тапсырысты әлі қабылдаған жоқ — болдырмау тегін.",
        "The shop hasn't accepted the order yet — cancelling is free.",
    ),
    "keep": ("Не отменять", "Болдырмау керек емес", "Keep the order"),
    "write_chat": ("Написать в чат", "Чатқа жазу", "Write to the chat"),
    "order_more": ("Заказать ещё", "Тағы тапсырыс беру", "Order more"),
    "err_closed": (
        "Заказы сейчас не принимаются — обратитесь на ресепшен",
        "Қазір тапсырыс қабылданбайды — ресепшенге хабарласыңыз",
        "Orders are not being accepted — please contact the reception",
    ),
    "err_sold_out": (
        "Что-то из корзины закончилось — проверьте заказ",
        "Себеттегі бір нәрсе таусылды — тапсырысты тексеріңіз",
        "Something in your cart is sold out — please check the order",
    ),
    "err_total": (
        "Цены изменились — проверьте итог",
        "Бағалар өзгерді — қорытындыны тексеріңіз",
        "Prices have changed — please check the total",
    ),
    "err_limit": (
        "Слишком много заказов — дождитесь текущих или напишите в чат",
        "Тапсырыс тым көп — ағымдағыларын күтіңіз немесе чатқа жазыңыз",
        "Too many orders — wait for the current ones or write to the chat",
    ),
    "err_accepted": (
        "Заказ уже принят — отменить можно через сотрудника, напишите в чат",
        "Тапсырыс қабылданып қойды — қызметкер арқылы болдырмауға болады, чатқа жазыңыз",
        "The order is already accepted — to cancel, write to us in the chat",
    ),
    "err_generic": (
        "Не получилось. Попробуйте ещё раз",
        "Болмады. Қайталап көріңіз",
        "Something went wrong. Please try again",
    ),
    # Причины отмены сотрудником — как их читает гость (spec 0038 §5).
    "reason_not_ordered": (
        "в номере этот заказ не подтвердили",
        "бұл тапсырыс расталмады",
        "the order was not confirmed in your room",
    ),
    "reason_no_answer": (
        "сотрудник не застал вас в номере",
        "қызметкер сізді нөмірден таба алмады",
        "our staff member couldn't reach you in your room",
    ),
    "reason_payment_refused": (
        "оплата не состоялась",
        "төлем жасалмады",
        "the payment did not go through",
    ),
    "reason_item_unavailable_restaurant": (
        "одного из блюд нет в наличии",
        "тағамдардың бірі қазір жоқ",
        "one of the dishes is not available",
    ),
    "reason_item_unavailable_shop": (
        "одного из товаров нет в наличии",
        "тауарлардың бірі қазір жоқ",
        "one of the items is not available",
    ),
    "reason_other_restaurant": (
        "ресторан не может выполнить заказ",
        "мейрамхана тапсырысты орындай алмайды",
        "the restaurant can't fulfil the order",
    ),
    "reason_other_shop": (
        "магазин не может выполнить заказ",
        "дүкен тапсырысты орындай алмайды",
        "the shop can't fulfil the order",
    ),
}


def text(key: str, language: str) -> str:
    """Надпись на языке гостя; язык не из трёх — английский."""
    ru, kk, en = PAGE_TEXTS[key]
    return {"ru": ru, "kk": kk}.get(language, en)


def page_texts_json() -> dict[str, dict[str, str]]:
    """Все надписи формой `{ключ: {ru, kk, en}}` — данные скрипта страницы."""
    return {key: {"ru": ru, "kk": kk, "en": en} for key, (ru, kk, en) in PAGE_TEXTS.items()}
