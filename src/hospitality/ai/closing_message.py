"""Сообщение гостю об исходе заявки, которое собирает модель (spec 0030, #58).

Частичное выполнение в домене — это `done` + `resolution_note` (spec 0021 П-4):
отдельного статуса нет и не будет. Но склейка «шаблон + приписка» врёт гостю
(«выполнена» и тут же «пылесос сломался») и показывает ему записку персонала,
написанную для коллеги. Поэтому такой текст собирается моделью заново: суть
заявки словами гостя + пометка персонала → одно живое сообщение сразу на языке
гостя.

Однозадачный вызов с единственным целевым языком — как в `ai/translation.py`
(урок бага #71: две задачи в одном вызове ненадёжны). Весь LLM-трафик — через
`ai/gateway` (§7.2). Границы «не выдумывать и не обещать» живут в самом промпте:
остаток работы сегодня не становится ничьей задачей, и обещание от лица отеля
исполнять некому (spec 0030, «Что сознательно НЕ делаем»).
"""

from __future__ import annotations

from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import LlmMessage, LlmProvider, LlmRequest
from hospitality.ai.prompts import load_prompt

# Версия промпта — в имени файла (§7.5).
PARTIAL_COMPLETION_PROMPT_NAME = "guest_partial_completion_v1"

# Плейсхолдер целевого языка; подстановка `str.replace`, не `str.format` — по той
# же причине, что в `ai/translation.py`: скобки в тексте промпта не синтаксис.
_LANGUAGE_PLACEHOLDER = "{language_code}"


async def compose_partial_completion(
    *,
    summary: str,
    resolution_note: str,
    language_code: str,
    provider: LlmProvider | None = None,
) -> str:
    """Текст гостю о частично выполненной заявке на его языке (spec 0030).

    `summary` — суть заявки словами гостя (как записана в заявке), `resolution_note`
    — пометка персонала по-русски. Ошибку провайдера (`AppError`) НЕ глотает и
    пустой ответ НЕ подменяет: деградацию к каноническому шаблону решает
    вызывающая сторона (`channels/telegram/notifications.py`), как и у перевода
    — уведомление гостю не должно исчезнуть из-за модели (§7.8).
    """
    system = load_prompt(PARTIAL_COMPLETION_PROMPT_NAME).replace(
        _LANGUAGE_PLACEHOLDER, language_code
    )
    # Метки по-английски — как системная инструкция: модель не должна принять их
    # за часть текста, который пишет гостю.
    content = f"Guest's request: {summary.strip()}\nStaff note (Russian): {resolution_note.strip()}"
    request = LlmRequest(messages=[LlmMessage(role="user", content=content)], system=system)
    response = await gateway.complete(request, provider=provider)
    return response.text.strip()
