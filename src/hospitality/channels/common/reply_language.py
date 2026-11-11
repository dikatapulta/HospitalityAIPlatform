"""Язык ответа гостю — счётчик «ответ не на языке гостя» (#1043 п. 1, spec 0035 §8.2).

Правило «отвечай на языке гостя» живёт только в промпте, а ответ иностранцу
по-русски — самый частый дефект живых прогонов. Здесь проверка, которая ответа
не блокирует: ход гостя после реплики сверяет алфавит ответа с алфавитом гостя,
пишет строку `guest_reply_language_checks` и учитывает счётчик
`guest_reply_language_checks_total`; копия утренней сводки основателю берёт
число за сутки (`count_reply_language`).

Сверяется только то, что проверка различает без библиотеки: латиница, кириллица
и прочие алфавиты (`shared/alphabet.py`). Русский и казахский — оба кириллица,
английский и турецкий — оба латиница. Не проверяется ход, где алфавит не ясен:
у гостя меньше `MIN_LETTERS` букв («ok», «wifi», «101») или ни у гостя, ни у
ответа нет алфавита с долей `MIN_SHARE` — смесь, как в трёхъязычных статических
текстах. Сигнал поэтому скорее промолчит, чем соврёт.

Канон файла — сосед `unanswered_questions.py`: запись и чтение таблицы рядом.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select

from hospitality.channels.common.models import GuestReplyLanguageCheck
from hospitality.shared.alphabet import Alphabet, dominant_alphabet
from hospitality.shared.db import session_scope
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_guest_reply_language

logger = get_logger(module=__name__)

# Меньше букв — алфавит гостя не решён: русский гость пишет «wifi» и «taxi»
# латиницей, а ответ ему по-русски верен. «Good morning» и «Где бассейн?» — уже 10+.
MIN_LETTERS: Final = 10
# Доля букв одного алфавита, с которой текст на нём написан. Ответ английскому
# гостю с русским названием ресторана — латиница; трёхъязычный текст (kk, ru, en
# с новой строки) — около 2/3 кириллицы, смесь, не проверяется.
MIN_SHARE: Final = 0.8


@dataclass(frozen=True)
class ReplyLanguageCheck:
    """Алфавиты одного хода; `mismatch` — ответ не алфавитом гостя."""

    guest: Alphabet
    reply: Alphabet

    @property
    def mismatch(self) -> bool:
        return self.guest is not self.reply


def compare_reply_language(guest_text: str, reply_text: str) -> ReplyLanguageCheck | None:
    """Сверить алфавиты гостя и ответа; алфавит одного из них не ясен — None."""
    guest = dominant_alphabet(guest_text, min_letters=MIN_LETTERS, min_share=MIN_SHARE)
    if guest is None:
        return None
    reply = dominant_alphabet(reply_text, min_letters=MIN_LETTERS, min_share=MIN_SHARE)
    if reply is None:
        return None
    return ReplyLanguageCheck(guest=guest, reply=reply)


async def check_reply_language(
    conversation_id: uuid.UUID, guest_text: str, reply_text: str
) -> None:
    """Проверить ответ хода и записать итог (внутри `tenant_context`, P-4).

    Зовётся ПОСЛЕ реплики гостю и не роняет ход: это сигнал, а не гейт. Сбой
    записи — WARNING с трейсбеком и `result=failed` в счётчике; реплика уже ушла,
    а исключение после неё ход гостя пробросил бы каналу (`run_guest_turn`).
    Текст гостя и ответа в лог не пишется (`docs/PII_REGISTRY.md`).
    """
    check = compare_reply_language(guest_text, reply_text)
    if check is None:
        return
    try:
        async with session_scope() as session:
            session.add(
                GuestReplyLanguageCheck(
                    guest_alphabet=check.guest.value, reply_alphabet=check.reply.value
                )
            )
    except Exception:
        logger.warning(
            "guest_reply_language_record_failed",
            conversation_id=str(conversation_id),
            exc_info=True,
        )
        record_guest_reply_language("failed")
        return
    record_guest_reply_language("mismatch" if check.mismatch else "match")
    if check.mismatch:
        logger.info(
            "guest_reply_language_mismatch",
            conversation_id=str(conversation_id),
            guest_alphabet=check.guest.value,
            reply_alphabet=check.reply.value,
        )


@dataclass(frozen=True)
class ReplyLanguageCount:
    """Проверенные ответы за окно и из них — не алфавитом гостя."""

    checked: int
    mismatched: int


async def count_reply_language(
    *, created_after: datetime, created_before: datetime
) -> ReplyLanguageCount:
    """Число проверок текущего тенанта в окне `[created_after, created_before)`.

    Окно приходит от сводки — модуль про сутки отеля не знает (канон
    `count_unanswered_questions`). Чужие строки отсекает RLS (P-4).
    """
    table = GuestReplyLanguageCheck
    async with session_scope() as session:
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.count().filter(table.guest_alphabet != table.reply_alphabet),
                ).where(table.created_at >= created_after, table.created_at < created_before)
            )
        ).one()
    return ReplyLanguageCount(checked=int(row[0]), mismatched=int(row[1]))
