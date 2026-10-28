"""«Спросить»: ответ по статьям кодекса потоком (spec 0048 §5.1–5.4).

Порядок хода: запрос поиска (короткое уточнение — вместе с прошлым вопросом) →
эмбеддинг → ближайшие статьи в пределах бюджета → промпт (инструкция, статьи,
резюме, история, вопрос) → поток ответа → ссылки на статьи → запись вопроса и
ответа → резюме старой истории → событие `done`.

Вопрос и ответ пишутся только после конца потока: оборванный ответ не
запоминается. Ошибки шлюза (ERR-AI-001/002/003/006) и ERR-MENTOR-002 «кодекс не
загружен» уходят вызывающему — текст сотруднику по коду выбирает кабинет (§7).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from typing import Final

from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.common import (
    CONTEXT_BUDGET_TOKENS,
    HISTORY_BUDGET_TOKENS,
    SEARCH_TOP_K,
    MentorDelta,
    MentorDone,
    MentorEvent,
    MentorProviders,
    article_block,
    articles_within_budget,
    cited_sources,
    embed_query,
    ensure_article_embeddings,
    estimate_tokens,
    require_codex,
    require_text,
    stream_reply,
)
from hospitality.ai.prompts import load_prompt
from hospitality.modules.mentor import api as mentor
from hospitality.shared.db import utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

ANSWER_PROMPT: Final = "mentor_answer_v1"
SUMMARY_PROMPT: Final = "mentor_summary_v1"
ANSWER_MAX_TOKENS: Final = 1024
SUMMARY_MAX_TOKENS: Final = 500
# Вопрос короче — уточнение («а если гость с ребёнком?»): ищем вместе с прошлым.
FOLLOW_UP_MAX_CHARS: Final = 80
# Сколько последних реплик остаётся дословно, даже если они одни больше бюджета.
KEEP_LAST_MESSAGES: Final = 2

_NOTHING_FOUND: Final = "(ничего не найдено)"


async def ask(
    user_id: uuid.UUID, message: str, *, providers: MentorProviders | None = None
) -> AsyncGenerator[MentorEvent, None]:
    """Ответ сотруднику: `MentorDelta`…, затем `MentorDone`. Внутри `tenant_context` (P-4).

    Потребитель закрывает поток через `contextlib.aclosing`: ушёл сотрудник — шлюз
    сразу пишет оборванный вызов, а диалог не меняется.
    """
    providers = providers or MentorProviders()
    question = message.strip()
    if not question:
        raise ValueError("ask: пустой вопрос")
    await require_codex()
    asked_at = utc_now()
    conversation = await mentor.get_conversation(user_id, mentor.ConversationMode.ASK)
    history = conversation.history if conversation else []
    previous = next((m.text for m in reversed(history) if m.role == mentor.MessageRole.USER), "")
    query = (
        f"{previous}\n{question}" if previous and len(question) < FOLLOW_UP_MAX_CHARS else question
    )

    await ensure_article_embeddings(providers)
    found = await mentor.search_articles(await embed_query(query, providers), limit=SEARCH_TOP_K)
    articles = articles_within_budget(found, CONTEXT_BUDGET_TOKENS)
    request = gateway.LlmRequest(
        system=_system_prompt(articles, conversation.summary if conversation else None),
        messages=[
            *(gateway.LlmMessage(role=m.role.value, content=m.text) for m in history),
            gateway.LlmMessage(role="user", content=question),
        ],
        max_tokens=ANSWER_MAX_TOKENS,
        purpose="mentor",
    )
    response: gateway.LlmResponse | None = None
    async with aclosing(stream_reply(request, providers)) as replies:
        async for item in replies:
            if isinstance(item, MentorDelta):
                yield item
            else:
                response = item
    answer = require_text(response, flow="ask")
    sources = await cited_sources(answer)
    conversation = await mentor.append_messages(
        user_id,
        mentor.ConversationMode.ASK,
        [
            mentor.NewMentorMessage(
                role=mentor.MessageRole.USER, text=question, created_at=asked_at
            ),
            mentor.NewMentorMessage(
                role=mentor.MessageRole.ASSISTANT,
                text=answer,
                cited_articles=[source.number for source in sources],
                created_at=utc_now(),
            ),
        ],
    )
    # Резюме — до `done`: пока его нет, кабинет держит кнопку «Спросить» заблокированной.
    await _compact_history(user_id, conversation, providers)
    logger.info(
        "mentor_answered",
        articles_found=len(found),
        articles_used=len(articles),
        sources=len(sources),
    )
    yield MentorDone(answer=answer, sources=sources)


def _system_prompt(articles: Sequence[mentor.CodexArticleRead], summary: str | None) -> str:
    blocks = "\n\n".join(article_block(article) for article in articles) or _NOTHING_FOUND
    prompt = f"{load_prompt(ANSWER_PROMPT).strip()}\n\nСтатьи кодекса:\n\n{blocks}"
    if summary:
        prompt += f"\n\nРезюме прошлой части разговора:\n{summary}"
    return prompt


async def _compact_history(
    user_id: uuid.UUID, conversation: mentor.ConversationRead, providers: MentorProviders
) -> None:
    """История больше бюджета — старые реплики пересказываются в резюме одним вызовом.

    Сбой модели здесь ответ не отменяет: он уже показан и записан. WARNING — и в
    следующий раз история снова окажется больше бюджета, резюме повторится.
    """
    rest = list(conversation.history)
    if len(rest) <= KEEP_LAST_MESSAGES or _tokens(rest) <= HISTORY_BUDGET_TOKENS:
        return
    old: list[mentor.MentorMessageRead] = []
    while len(rest) > KEEP_LAST_MESSAGES and _tokens(rest) > HISTORY_BUDGET_TOKENS:
        old.append(rest.pop(0))
    transcript = "\n".join(
        f"{'Сотрудник' if m.role == mentor.MessageRole.USER else 'Наставник'}: {m.text}"
        for m in old
    )
    content = f"Новые реплики:\n{transcript}"
    if conversation.summary:
        content = f"Предыдущее резюме:\n{conversation.summary}\n\n{content}"
    try:
        response = await gateway.complete(
            gateway.LlmRequest(
                system=load_prompt(SUMMARY_PROMPT).strip(),
                messages=[gateway.LlmMessage(role="user", content=content)],
                max_tokens=SUMMARY_MAX_TOKENS,
                purpose="mentor",
            ),
            provider=providers.chat,
        )
    except AppError as error:
        logger.warning("mentor_summary_failed", error_code=error.code)
        return
    summary = response.text.strip()
    if not summary:
        logger.warning("mentor_summary_failed", error_code=None)
        return
    await mentor.compact_history(
        user_id,
        mentor.ConversationMode.ASK,
        message_ids=[m.id for m in old],
        summary=summary,
    )


def _tokens(messages: Sequence[mentor.MentorMessageRead]) -> int:
    return sum(estimate_tokens(m.text) for m in messages)
