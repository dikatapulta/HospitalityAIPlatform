"""Тексты сотруднику на экранах записок (spec 0077 §9; новые — запись founder-review PR #1004).

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
BY_PASSWORD: Final = "подтверждено паролем кабинета, {when}"
BY_DISOWNED: Final = "подтверждено с устройства, которое владелец потом назвал чужим, {when}"
BY_RESET_REVOKED: Final = (
    "подтверждено с устройства, которое потом отвязал сброс пароля {day}, {when}"
)
BY_MISMATCH: Final = "подтверждение не сходится с нынешним текстом записки, {when}"
PASSWORD_WAS_RESET: Final = "пароль перед этим сбрасывали {day}"
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
