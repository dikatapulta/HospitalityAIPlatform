"""Вопрос гостя без ответа в справочнике — запись строки (spec 0036 §6).

Канон — сосед `events.py`: факт целиком живёт в одном файле, а не размазан
между `store.py` и потребителем. Разница ровно одна и она по делу: у эскалации
факт двусоставный (событие в outbox + строка), поэтому обе половины пишутся
одной транзакцией; здесь события нет — человека звать не нужно (эскалацией это
не является, spec 0022), нужна только строка для страницы «Справочник отеля».

В `store.py` запись не уехала по R-3: тот файл уже на границе ~400 строк.
Чтение (список вопросов за окно страницы) добавит сюда же PR D (#335) — оба
обращения к таблице обязаны меняться вместе.

Пишет строку КАНАЛ (`channels/common/guest_turn.py`) по полю исхода
`OrchestratorTurn.unanswered_question`, буква в букву как `escalation`: таблица
лежит здесь, рядом с `conversation_escalations` — своим близнецом по смыслу и по
запросу со страницы кабинета. Машинной проверки у этой границы нет: направление
`ai → channels` контрактом импорт-линтера сегодня не закрыто (в контракте слоёв
`pyproject.toml` `ai`, `channels` и `staff_portal` перечислены через «:», то есть
как сиблинги с разрешёнными взаимными импортами). Держит канон, а не линтер.
"""

from __future__ import annotations

import uuid

from hospitality.channels.common.models import UnansweredQuestion
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def record_unanswered_question(conversation_id: uuid.UUID, question: str) -> None:
    """Записать один вопрос без ответа (внутри `tenant_context`, P-4).

    Своя транзакция на вызов — канон `store.py`: бизнес-записи, с которой её
    нужно было бы связать одним коммитом, здесь нет (в отличие от эскалации, где
    строка и событие обязаны коммититься вместе).

    Идемпотентность (P-8) своего ключа не требует: повторную доставку того же
    сообщения гостя отсекает выше по потоку уникальность
    `messages.idempotency_key`, а два РАЗНЫХ хода с одним и тем же вопросом —
    это две строки намеренно: повтор и есть сигнал частоты (§10 спеки).

    Текст вопроса в лог не пишется — это текст гостя (`docs/PII_REGISTRY.md`).
    """
    async with session_scope() as session:
        session.add(UnansweredQuestion(conversation_id=conversation_id, question=question))
    logger.info("unanswered_question_recorded", conversation_id=str(conversation_id))
