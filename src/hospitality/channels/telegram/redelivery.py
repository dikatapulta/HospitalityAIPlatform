"""Дожим ответа в чат, не ушедшего с первой попытки (issue #209, spec 0016 §P-8).

До этой задачи сбой `sendMessage` в `outbound.send_reply` съедался WARNING'ом:
заявка создана, токены потрачены, персонал уведомлён — а гость видел тишину.
Повтор апдейта Telegram гасила дедупликация входящего (P-8), WARNING в событие
Sentry не превращался, и о потере не узнавал никто. Штатный 429 Bot API
(1 сообщение в секунду на чат) делал это регулярным, а после spec 0034 этим же
путём уходит ответ ЧП-перехвата.

**Кого дожимает — не только гостя.** Через `send_reply` идёт и ответ персоналу в
чат службы (`staff.py`: исход `/done`, `/take`, ответ на вопрос «что не сделано?»),
поэтому событие названо по каналу, а не по адресату: адресата различает `chat_id`
(и `conversation_id`), а не имя события. Срок годности и код потери у обоих
направлений общие — реплика персоналу так же привязана к ходу разговора в группе.

**Выбран outbox, а не «громкий best-effort».** Тот же довод, которым spec 0022 §2
выбрала событие для эскалации: сбой отправки обязан переживаться ретраем, а не
записью в лог. Реплика, не ушедшая с первой попытки, коммитится в outbox и
дожимается воркером (`shared/events`): первая повторная попытка — через
`WORKER_RETRY_BACKOFF_BASE_SECONDS` (2 с), дальше экспонента (ADR-009). Сеть при
этом лежит вне транзакции (ADR-016), как у всякой доставки outbox.

**Путей окончательной потери два, и оба — ERROR `ERR-TELEGRAM-006`:** реплика не
встала в очередь (`reason=queue_failed`) либо протухла в ней (`reason=too_stale`).
Похорон (dead-letter, ADR-015) у этого события при штатных настройках не бывает:
единственная попытка, на которой `attempts >= WORKER_MAX_DELIVERY_ATTEMPTS` (10),
— десятая, а по расписанию backoff она приходится на 810-ю секунду, где реплика
уже протухла. Подписчик там возвращается, а не бросает, поэтому `dead_lettered_at`
не выставляется никогда и `ERR-EVENTS-002` по этому событию не приходит. Путь
оживает, как только ПОСЛЕДНЯЯ по счёту попытка попадает внутрь срока годности, а
задают её расписание три настройки, не одна: `WORKER_MAX_DELIVERY_ATTEMPTS` ≤ 9
(девятая попытка идёт на 510-й секунде), `WORKER_RETRY_BACKOFF_MAX_SECONDS` ниже
≈ 170 с при нетронутом пределе (при 100 с десятая попытка приходится на 426-ю
секунду) или база ниже ≈ 1,2 с. Хватает любой одной; трогали любую — считать
расписание заново (разбор — `docs/runbooks/errors.md`, ERR-TELEGRAM-006).

Терминальная отметка протухшей реплики — `processed_at`, ТА ЖЕ, что у доставленной:
в outbox потеря неотличима от успеха, и единственный её след — ERROR в логе и
Sentry. Считать потери по таблице нельзя, только по коду ошибки (метрика — #294).

**Протухание.** У реплики, в отличие от уведомления, есть срок годности: она
привязана к ходу диалога, и «принял, полотенце несут» через полчаса дезориентирует
сильнее, чем молчание. Поэтому подписчик перед отправкой сверяет возраст реплики с
`REPLY_MAX_AGE_SECONDS`: старше — не отправляется вовсе, а становится ERROR
`ERR-TELEGRAM-006` (Sentry, §10.4). Тишина у гостя при этом остаётся — но она
перестаёт быть незамеченной, и это вторая половина решения issue.

**История диалога.** Инвариант прежний и усилен: исходящий `Message` пишется
ТОЛЬКО после фактической отправки — здесь, в момент удавшегося дожима. История
остаётся тем, что гость действительно видел, и в неё встаёт правильный момент
времени (дожима, а не первой попытки). Не дошедшая реплика в историю не попадает
никогда: ни при отказе очереди, ни при протухании.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar

from hospitality.channels.common.store import (
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.telegram.client import TelegramSender
from hospitality.channels.telegram.routing import current_correlation_id
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.events import DomainEvent, publish, subscribe
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога ошибок (docs/runbooks/errors.md, R-8): ответ в чат — гостю или
# персоналу — потерян ОКОНЧАТЕЛЬНО: либо не встал в очередь, либо протух в ней.
ERR_TELEGRAM_REPLY_LOST = "ERR-TELEGRAM-006"

# Срок годности реплики; владелец числа — spec 0016 §Идемпотентность (правка здесь
# без правки спеки = расхождение, его держит тест `test_deadline_value_is_pinned`).
# Дожим внутри срока полезен (гость ещё ждёт ответа на своё сообщение), позже —
# вреден: разговор ушёл вперёд, а старая реплика приходит в него как чужая.
# 10 минут — окно, в которое backoff ADR-009 укладывает 9 попыток: на 0, 2, 6, 14,
# 30, 62, 126, 254 и 510-й секунде (восемь пауз 2+4+8+16+32+64+128+256 с между
# ними), то есть переживает и 429, и обычный сбой Bot API. Что дольше десяти минут
# — уже не сбой доставки, а недоступность канала, и о ней человеку говорит
# ERR-TELEGRAM-006, а не запоздалое сообщение гостю.
REPLY_MAX_AGE_SECONDS = 600.0


class TelegramReplyUndelivered(DomainEvent):
    """Факт «реплика в чат не ушла с первой попытки» — её обязан дожать воркер.

    `idempotency_key` не опционален (в отличие от `record_outbound_message`):
    доставка outbox — at-least-once (ADR-005), и без ключа повторно доставленное
    событие отправило бы гостю второй экземпляр. У реплик с естественным ключом
    (приветствие после согласия, spec 0029 §4) он проносится сюда как есть — так
    дожим не обходит их собственную идемпотентность; у обычных реплик хода ключ
    синтезируется при постановке в очередь (`queue_undelivered_reply`).
    """

    event_name: ClassVar[str] = "telegram_reply.undelivered"

    conversation_id: uuid.UUID
    chat_id: str
    text: str
    idempotency_key: str
    # Момент первой (неудавшейся) попытки — от него считается протухание.
    # В payload, а не из `outbox_events.occurred_at`: подписчик видит событие,
    # а не строку, и лезть за ней значило бы протащить в него устройство outbox.
    queued_at: datetime


async def queue_undelivered_reply(
    conversation_id: uuid.UUID,
    chat_id: str,
    text: str,
    *,
    idempotency_key: str | None,
) -> None:
    """Поставить не ушедшую реплику в outbox (внутри `tenant_context` канала).

    Собственная транзакция: бизнес-записи, с которой её можно было бы разделить,
    здесь нет — ход гостя уже закоммичен, не хватает только доставки.

    Не бросает: вызывается из обработчика вебхука, а 500 в ответ Telegram'у
    ничего не чинит (повтор апдейта съест дедупликация входящего) и вдобавок
    откатил бы уже сделанную работу хода. Упасть может только БД — и тогда
    реплика потеряна окончательно: это ERROR `ERR-TELEGRAM-006`, а не WARNING.
    """
    event = TelegramReplyUndelivered(
        conversation_id=conversation_id,
        chat_id=chat_id,
        text=text,
        # Синтетический ключ — на реплику, а не на диалог: у двух подряд реплик
        # одного хода (и у двух ходов подряд) тексты бывают одинаковые, и общий
        # ключ проглотил бы вторую как дубль первой. Префикс — по каналу, а не по
        # адресату (`guest:`/`staff:` у прочих ключей): через `send_reply` идут
        # оба, и в чате службы ключ `guest:` врал бы читающему psql так же, как
        # врало бы имя события.
        idempotency_key=idempotency_key or f"telegram:reply:{uuid.uuid4()}",
        queued_at=utc_now(),
    )
    try:
        async with session_scope() as session:
            await publish(session, event)
    except Exception:
        logger.error(
            "telegram_reply_lost",
            error_code=ERR_TELEGRAM_REPLY_LOST,
            reason="queue_failed",
            conversation_id=str(conversation_id),
            chat_id=chat_id,
            exc_info=True,
        )
        return
    logger.warning(
        "telegram_reply_queued",
        conversation_id=str(conversation_id),
        chat_id=chat_id,
    )


async def redeliver_reply(event: TelegramReplyUndelivered, *, sender: TelegramSender) -> None:
    """Подписчик `telegram_reply.undelivered`: дожать реплику или объявить её потерянной.

    Сбой отправки ПРОБРАСЫВАЕТСЯ — это и есть ретрай: воркер отложит событие по
    backoff (ADR-009) и повторит. Идемпотентность (P-8) — на естественном ключе
    реплики: повторная доставка события находит уже записанный исходящий
    `Message` и выходит.

    Порядок двух проверок строгий: сначала факт доставки, потом возраст. «Уже
    отправлено» — знание о результате, «слишком старо» — лишь предположение о
    нём. Событие возвращается на доставку и после того, как реплика ушла И
    записана: строка outbox не получила `processed_at` (процесс умер между
    записью `Message` и фиксацией исхода, истекла аренда
    `WORKER_DELIVERY_LEASE_SECONDS`) — а на поздних попытках это уже за
    десятиминутной границей. При обратном порядке такая реплика получила бы
    ERROR «ответ потерян окончательно», после чего рунбук послал бы человека
    отправить гостю то же самое второй раз.

    Обратное гард не закрывает и не может: умри процесс МЕЖДУ отправкой и
    записью `Message`, следов доставки у платформы нет вовсе — протухшая реплика
    станет `too_stale`, честно для платформы и неверно для гостя, который её
    получил. Гард отвечает за то, что записано, а не за всё, что дошло.
    """
    if await notification_already_sent(event.idempotency_key):
        logger.info("telegram_reply_redelivery_skipped", chat_id=event.chat_id)
        return

    age_seconds = (utc_now() - event.queued_at).total_seconds()
    if age_seconds > REPLY_MAX_AGE_SECONDS:
        # Возврат, а не исключение: событие получает `processed_at` — ту же
        # терминальную отметку, что и доставленное (в outbox эта потеря
        # неотличима от успеха, её видно только по коду ошибки). Повторять
        # нечего: от следующей попытки реплика только состарится.
        logger.error(
            "telegram_reply_lost",
            error_code=ERR_TELEGRAM_REPLY_LOST,
            reason="too_stale",
            conversation_id=str(event.conversation_id),
            chat_id=event.chat_id,
            age_seconds=round(age_seconds),
        )
        return

    sent_id = await sender.send_message(event.chat_id, event.text)
    # Запись — только после успешной отправки (история диалога не врёт) и она же
    # гасит повтор: гонку двух доставок закрывает уникальное ограничение
    # `messages (tenant_id, idempotency_key)`.
    await record_outbound_message(
        event.conversation_id,
        event.text,
        current_correlation_id(),
        external_message_id=sent_id,
        idempotency_key=event.idempotency_key,
    )
    logger.info(
        "telegram_reply_redelivered",
        conversation_id=str(event.conversation_id),
        chat_id=event.chat_id,
        age_seconds=round(age_seconds),
    )


def register(*, sender: TelegramSender) -> None:
    """Подписать дожим на своё событие (зовётся composition root'ом воркера).

    Отдельно от `notifications.register`: там подписчики доменных событий
    («служба узнаёт о заявке»), здесь — транспортная страховка канала. Отправитель
    у них общий, замыкание связывает его с обработчиком так же (P-6: событие
    отправителя не несёт).
    """

    async def on_telegram_reply_undelivered(event: TelegramReplyUndelivered) -> None:
        await redeliver_reply(event, sender=sender)

    subscribe(TelegramReplyUndelivered, on_telegram_reply_undelivered)
