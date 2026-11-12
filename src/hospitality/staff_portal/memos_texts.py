"""Тексты сотруднику на экранах записок (spec 0077 §9; новые — записи founder-review PR #1004,
#1020, #1090 — объяснительные — и #1097 — «Оформить» и «Шаблоны записок»).

Один файл на все надписи записок: страница, окно подтверждения (через `data-msg-*`)
и пуши берут их отсюда, а не сочиняют сами.
"""

from __future__ import annotations

from typing import Final

from hospitality.modules.memos import api as memos_api

STATUS: Final = {
    memos_api.MemoStatus.DRAFT: "Черновик",
    memos_api.MemoStatus.SIGNING: "На подписи",
    memos_api.MemoStatus.SUBMITTED: "У руководителя",
    memos_api.MemoStatus.APPROVED: "Согласовано",
    memos_api.MemoStatus.REJECTED: "Отказано",
    memos_api.MemoStatus.RETURNED: "На доработку",
    memos_api.MemoStatus.WITHDRAWN: "Отозвана",
}
ROLE: Final = {
    memos_api.SignatureRole.AUTHOR: "Автор",
    memos_api.SignatureRole.SIGNER: "Подписант",
    memos_api.SignatureRole.ADDRESSEE: "Руководитель",
}
EMPTY: Final = {
    memos_api.MemoBox.WAITING: "Сейчас ничего не ждёт вашей подписи или решения.",
    memos_api.MemoBox.MINE: "Вы ещё не писали записок.",
    memos_api.MemoBox.ALL: "Записок с вашим участием пока нет.",
}

# Лист подписей (§9): ответ строки.
SENT: Final = "Отправил(а)"
SIGNED: Final = "Подписал(а)"
SIGNED_WITH_REMARK: Final = "Подписал(а) с замечанием"
NO_ANSWER: Final = "Не ответил(а) к моменту отправки"
WAITING: Final = "Ждём ответа"
# Лист подписей (§9, §4): способ подтверждения; {when} — дата и время по поясу отеля.
BY_DEVICE: Final = "подтверждено с личного устройства, {when}"
BY_PASSWORD: Final = "подтверждено паролем кабинета, {when}"  # noqa: S105 — текст экрана, не пароль
BY_DISOWNED: Final = "подтверждено с устройства, которое владелец потом назвал чужим, {when}"
BY_RESET_REVOKED: Final = (
    "подтверждено с устройства, которое потом отвязал сброс пароля {day}, {when}"
)
BY_MISMATCH: Final = "подтверждение не сходится с нынешним текстом записки, {when}"
PASSWORD_WAS_RESET: Final = "пароль перед этим сбрасывали {day}"  # noqa: S105 — текст экрана, не пароль
KEY_AFTER_RESET: Final = "устройство привязано после сброса пароля {day}"
CONTENT_CHANGED: Final = (
    "Текст записки изменён после отправки — подтверждения ниже относятся к прежнему тексту."
)

# Действия на карточке (§9).
SIGN_REQUEST: Final = "{author} просит подписать записку."
DECISION_HINT: Final = (
    "„Согласовано“ — решение по записке, а не приказ. Приказ, распоряжение и график "
    "оформляются как обычно."
)
DRAFT_HINT: Final = "Записка уйдёт всем сразу. Отправить руководителю можно, не дожидаясь всех."
SUBMIT_CONFIRM: Final = (
    "Отправить руководителю? Кто не ответил, останется в листе строкой "
    "«не ответил(а) к моменту отправки»."
)
WITHDRAW_CONFIRM: Final = "Отозвать записку? Номер останется занятым, подписи больше не собираются."
SEND_TO_SIGNERS: Final = "Отправить на подпись"
SEND_TO_ADDRESSEE: Final = "Отправить руководителю"
TEMPLATE_CONFIRM: Final = "Заменить текст шаблоном?"
FREE_FORM: Final = "Свободная форма"

# «Оформить» в черновике (§7, §9; запись founder-review PR #1097).
FORMAT_BUTTON: Final = "Оформить"
FORMAT_BUSY: Final = "Оформляю…"
FORMAT_HINT: Final = "Оформить — ИИ поправит стиль и ошибки и ничего не добавит от себя."
FORMAT_RESULT: Final = "Так оформил ИИ — проверьте"
FORMAT_ACCEPT: Final = "Принять"
FORMAT_UNDO: Final = "Вернуть мой текст"
# Отказы «Оформить» для JS (`data-msg-*`): по коду ошибки шлюза, пустое поле — до запроса.
FORMAT_MESSAGES: Final = {
    "empty": "Сначала напишите текст — ИИ его оформит.",
    "budget": "На сегодня оформлений больше нет. Оформите текст сами или попробуйте завтра.",
    "incomplete": "ИИ не смог оформить текст целиком. Сократите его или оформите сами.",
    "unavailable": "ИИ сейчас не отвечает. Попробуйте через минуту или оформите текст сами.",
}

# «Шаблоны записок» — экран менеджера (§7; запись founder-review PR #1097).
TEMPLATES_TAB: Final = "Шаблоны"
TEMPLATES_TITLE: Final = "Шаблоны записок"
TEMPLATES_INTRO: Final = (
    "Шаблон подставляет текст в новую записку — сотрудник дописывает своё. «Свободная форма» "
    "есть всегда. До {count} шаблонов."
)
TEMPLATES_HINT: Final = (
    "Удалённый шаблон пропадёт из выбора в новых записках. Уже написанные записки не изменятся."
)
TEMPLATE_NAME: Final = "Название"
TEMPLATE_BODY: Final = "Текст"
TEMPLATE_ADD: Final = "Добавить шаблон"
TEMPLATE_DELETE: Final = "Удалить"
TEMPLATES_SAVE: Final = "Сохранить"
TEMPLATES_MESSAGES: Final = {
    "saved": "Сохранено.",
    "incomplete": "У каждого шаблона нужны название и текст.",
    "limit": "Не больше {count} шаблонов.",
    "failed": "Не получилось сохранить — обновите страницу и попробуйте ещё раз.",
}

# Ответы действий — показывает JS по коду и причине отказа (`data-msg-*`).
MESSAGES: Final = {
    "confirm_title": "Подтвердите, что это вы",
    "confirm_device": "Подтвердить на устройстве",
    "confirm_password": "Ввести пароль кабинета",
    "confirm_failed": "Не получилось подтвердить. Попробуйте ещё раз или введите пароль.",
    "expired": "Окно подтверждения устарело — нажмите кнопку ещё раз.",
    "password": "Пароль не подошёл.",
    "too_many_attempts": "Слишком много попыток. Подождите и попробуйте снова.",
    "no_sample": "Сначала настройте подпись в разделе «Моя подпись».",
    "changed": "Запись уже изменилась — обновите страницу.",
    "comment_required": (
        "Напишите комментарий — без него «Отказано» и «На доработку» не отправить."
    ),
    "draft_saved": "Черновик сохранён.",
    "fill_title_body": "Заполните заголовок и текст.",
    "pick_addressee": "Выберите, кому записка.",
    "members_changed": (
        "Кто-то из выбранных больше не работает в отеле или не может быть адресатом — "
        "проверьте список."
    ),
    "failed": "Не получилось. Попробуйте ещё раз.",
}

# Пуши (§9): без заголовка записки и без решения — их видно на заблокированном экране.
PUSH_TITLE: Final = "Служебные записки"
PUSH_SIGN: Final = "Служебная записка ждёт вашей подписи"
PUSH_RECEIVED: Final = "Вам поступила служебная записка"
PUSH_DECIDED: Final = "По вашей записке принято решение"
PUSH_DEMAND: Final = "Вам вручено требование о письменном объяснении — откройте кабинет"
PUSH_EXPLANATION: Final = "Подана объяснительная"

# Вид записи (§2, §6): фильтр реестра и шапка печати.
KIND: Final = {
    memos_api.MemoKind.MEMO: "Служебная записка",
    memos_api.MemoKind.EXPLANATION: "Объяснительная",
}

# Поиск и фильтры реестра (§5; новые — запись founder-review PR #1020).
SEARCH_TITLE: Final = "Поиск и фильтры"
SEARCH_HINT: Final = "Ищем только среди записок с вашим участием. По тексту записки поиска нет."
SEARCH_ANY: Final = "Любой"
SEARCH_FOUND: Final = "Найдено: {count}"
SEARCH_EMPTY: Final = "Ничего не нашлось. Измените условия или нажмите «Сбросить»."

# Печать (§5, §9; новые — запись founder-review PR #1020).
PRINT_BUTTON: Final = "Печать / сохранить в PDF"
PRINT_HINT: Final = (
    "Чтобы получить PDF, в окне печати выберите «Сохранить как PDF» вместо принтера."
)
PRINT_FOOTER: Final = (
    "Сформировано в Necturn {day}. Подтверждения — по Положению о служебных записках Отеля; "
    "запись не является электронным документом с ЭЦП."
)

# Объяснительные (§6, §9; новые — запись founder-review PR #1090).
EXPLANATION_STATUS: Final = {**STATUS, memos_api.MemoStatus.SUBMITTED: "Подана"}
EXPLANATION_SENT: Final = "Подал(а)"
SEND_EXPLANATION: Final = "Подать"
BASED_ON: Final = {
    memos_api.MemoKind.MEMO: "Взамен",
    memos_api.MemoKind.EXPLANATION: "Дополнение к",
}
BY_DEMAND: Final = "По требованию от {day}"
LATE: Final = "Подана после срока — срок был до конца дня {day}."
NO_DEMAND_NOTE: Final = (
    "Для дисциплинарного взыскания нужно письменное требование — эта запись его не заменяет."
)
EXPLANATION_NEW: Final = "Новая объяснительная"
EXPLANATION_DRAFT: Final = "Черновик объяснительной"
SUPPLEMENT_TITLE: Final = "Дополнение к {number}"
# Имя из базы — в именительном падеже, поэтому после двоеточия (ответ диспетчера 12.11).
EXPLANATION_TO: Final = "Объяснительная уйдёт тому, кто затребовал объяснение: {name}."
EXPLANATION_ADDRESSEE: Final = "Кому: {name}"
EXPLANATION_FINAL: Final = "Поданную объяснительную изменить нельзя — можно дополнить."
SUPPLEMENT: Final = "Дополнить"

# Требование объяснения (§6, §9).
DEMAND_TITLE: Final = "Требование объяснения"
DEMAND_NEW: Final = "Затребовать объяснение"
DEMAND_INTRO: Final = (
    "Сначала вручите сотруднику бумажное требование с вашей подписью. Здесь приложите его "
    "фото: сотрудник увидит требование в кабинете, а вы — когда он подтвердил получение."
)
DEMAND_PHOTO_HINT: Final = "Снимите лист целиком, чтобы читались текст и подпись."
DEMAND_SENIOR_HINT: Final = "Старший требует объяснение только у сотрудников своих служб."
DEMAND_SEND: Final = "Отправить требование"
DEMAND_STATE: Final = {
    memos_api.DemandState.AWAITING_RECEIPT: "Не получено",
    memos_api.DemandState.OPEN: "Получено, срок до {day}",
    memos_api.DemandState.EXPIRED: "Срок истёк {day}",
    memos_api.DemandState.EXPLAINED: "Объяснение подано",
}
# Сотруднику, у которого требуют (§9; «Откройте требование и» снято — он уже на странице).
DEMAND_TO_RECEIVE: Final = (
    "{boss} требует письменное объяснение. Нажмите „Получил(а)“ — с этого момента у вас "
    "два рабочих дня."
)
DEMAND_OPEN: Final = "Срок — до конца дня {day}. Писать объяснение или нет — ваше право."
DEMAND_EXPIRED_EMPLOYEE: Final = (
    "Срок истёк {day}. Объяснение можно подать и сейчас — с отметкой „после срока“. "
    "Писать или нет — ваше право."
)
# Затребовавшему (§6).
DEMAND_NOT_RECEIVED: Final = "Получение не подтверждено — вручите требование лично."
DEMAND_RECEIVED: Final = "Сотрудник подтвердил получение. Срок — до конца дня {day}."
DEMAND_EXPIRED_BOSS: Final = (
    "Срок истёк {day}. Акт о непредставлении объяснения составляется на бумаге, вне системы."
)
DEMAND_EXPLAINED: Final = "Объяснение подано."
RECEIVED_LINE: Final = "Получение: {method}"
RECEIPT_MISMATCH: Final = "подтверждение не сходится с нынешним требованием, {when}"
RECEIVE_BUTTON: Final = "Получил(а)"
WRITE_EXPLANATION: Final = "Написать объяснительную"
EXPLANATION_SUBMITTED: Final = "подана {when}"
EXPLANATION_LATE: Final = "после срока"
DEMAND_MESSAGES: Final = {
    "pick_employee": "Выберите сотрудника.",
    "pick_photo": "Приложите фото требования.",
    "too_large": "Фото больше 5 МБ — сожмите его на телефоне и загрузите снова.",
    "wrong_file": "Такой файл не подходит. Нужна фотография: JPG, PNG или WebP.",
    "storage": "Фото сейчас не сохранить — попробуйте позже.",
    "not_your_service": ("Этот сотрудник не из ваших служб — требование может отправить менеджер."),
}


def status_text(kind: memos_api.MemoKind, status: memos_api.MemoStatus) -> str:
    """Статус по виду: поданная объяснительная — «Подана», а не «У руководителя» (§6)."""
    table = EXPLANATION_STATUS if kind is memos_api.MemoKind.EXPLANATION else STATUS
    return table[status]
