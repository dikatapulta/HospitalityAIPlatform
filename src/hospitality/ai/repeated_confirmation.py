"""Гейт P-9 не задаёт гостю тот же вопрос второй раз (issue #858, ADR-018).

Модель иногда спрашивает «Оформить заявку…?» свободным текстом, без вызова
инструмента, — вопреки `concierge_v7`. Гость отвечает «да», модель зовёт
инструмент на этом ходу, и гейт задал бы гостю тот же вопрос снова (на прогоне
показа #848 — 2 заявки из 6). Если прямо перед репликой гостя бот написал
ровно тот вопрос, который гейт задал бы сейчас, а классификатор гейта признал
реплику подтверждением, — гость уже ответил «да» на этот вопрос: P-9 соблюдён,
и оркестратор исполняет действие без второго вопроса.

Здесь — только сравнение текстов, без LLM. Классификатор и исполнение — те же
функции оркестратора, что у хода с `pending_action`: путь «да» один, и лог
у него один (`pending_action_confirmed`). Совпадение — строгое: вопрос
с другой ценой, службой или количеством (`prepare` квоты, spec 0056 §4) уже
не совпадает, и гость увидит его прежде, чем что-то исполнится.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from hospitality.ai.gateway.api import LlmMessage


def asked_just_before(history: Sequence[LlmMessage] | None, question: str) -> bool:
    """Последнее сообщение истории — исходящее и совпадает с `question`.

    Последнее сообщение истории стоит прямо перед текущей репликой гостя:
    оркестратор добавляет её сам (`handle_message`). Гость написал два
    сообщения подряд — последним в истории будет его же реплика, и вопрос
    гейт задаст, как раньше.
    """
    if not history or history[-1].role != "assistant":
        return False
    expected = _normalized(question)
    return bool(expected) and _normalized(history[-1].content) == expected


def _normalized(text: str) -> str:
    """Регистр снят, пробелы схлопнуты, конечная пунктуация отброшена."""
    collapsed = " ".join(text.split()).casefold()
    end = len(collapsed)
    while end and (
        collapsed[end - 1].isspace() or unicodedata.category(collapsed[end - 1]).startswith("P")
    ):
        end -= 1
    return collapsed[:end]
