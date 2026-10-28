"""Общее для хода вопроса и «Тренировки» (spec 0048 §5): события потока, бюджеты,
поиск статей, ссылки на статьи.

Бюджеты и оценка токенов — константы «Наставника» (`hotelbot/config.py`,
`tokens.py`), без новой зависимости-токенизатора (spec 0048 §3).
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel

from hospitality.ai.gateway import api as gateway
from hospitality.modules.mentor import api as mentor
from hospitality.shared.config import get_settings
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Статьи кодекса в промпте ответа — не больше стольких токенов.
CONTEXT_BUDGET_TOKENS: Final = 6000
# История диалога дословно — не больше стольких токенов; старше — в резюме.
HISTORY_BUDGET_TOKENS: Final = 2500
# Сколько ближайших статей приносит поиск до отсечки бюджетом.
SEARCH_TOP_K: Final = 10
# Предел вопроса и ответа «Тренировки» сотрудника, знаков (ревью PR #512, Н-11).
# Вопрос — текст эмбеддинга (предел модели — 8192 токена), уточнение ищется вместе
# с прошлым вопросом: 2000 + 2000 знаков укладываются с запасом. Длиннее — не
# вопрос о стандарте, а вставленный документ.
MAX_MESSAGE_CHARS: Final = 2000

_CITATION = re.compile(r"\[Статья (\d+(?:-\d+)?)\]")


def estimate_tokens(text: str) -> int:
    """Грубая оценка с запасом: у русского текста 1 токен — это 3–4 символа."""
    return max(1, len(text) // 3) if text else 0


class MentorSource(BaseModel):
    """Статья под ответом: «Источник: Статья 30. Встреча гостя» — ссылка на неё."""

    number: str
    title: str
    anchor: str


class MentorDelta(BaseModel):
    """Очередной кусок ответа — сотрудник видит его сразу."""

    type: Literal["delta"] = "delta"
    text: str


class MentorDone(BaseModel):
    """Последнее событие: весь ответ и его источники; диалог к этому моменту записан."""

    type: Literal["done"] = "done"
    answer: str
    sources: list[MentorSource]


MentorEvent = MentorDelta | MentorDone


@dataclass(frozen=True)
class MentorProviders:
    """Подмена провайдеров шлюза Fake-адаптерами в тестах (ADR-007).

    Бизнес-код их не передаёт: шлюз берёт боевые провайдеры моделей назначения
    `mentor` (`LLM_MODEL_MENTOR`, `LLM_EMBEDDING_MODEL` — ADR-024).
    """

    chat: gateway.LlmProvider | None = None
    streaming: gateway.LlmStreamingProvider | None = None
    embeddings: gateway.EmbeddingProvider | None = None


async def require_codex() -> None:
    """Кодекс не загружен — ERR-MENTOR-002: отвечать не по чему (spec 0048 §7)."""
    if not await mentor.has_codex():
        raise AppError(
            code=mentor.ERR_MENTOR_CODEX_MISSING,
            message="Codex is not uploaded for this hotel",
            status_code=409,
        )


async def ensure_article_embeddings(providers: MentorProviders) -> None:
    """Досчитать векторы статей без вектора или с вектором другой модели.

    Так кодекс, засеянный без модели (демо-тенант staging), или кодекс после смены
    `LLM_EMBEDDING_MODEL` начинает искаться на первом же вопросе — пересчёт кодекса
    стоит доли цента (spec 0048 §8). Обычно статей таких нет и вызова модели нет.
    """
    model = get_settings().llm_embedding_model
    pending = await mentor.list_articles_needing_embedding(model)
    if not pending:
        return
    response = await gateway.embed(
        [article.full_text for article in pending], purpose="mentor", provider=providers.embeddings
    )
    await mentor.store_article_embeddings(
        {article.id: vector for article, vector in zip(pending, response.vectors, strict=True)},
        model=response.model,
    )
    logger.info("mentor_embeddings_backfilled", articles=len(pending))


async def embed_query(text: str, providers: MentorProviders) -> list[float]:
    response = await gateway.embed([text], purpose="mentor", provider=providers.embeddings)
    return response.vectors[0]


def articles_within_budget(
    articles: Sequence[mentor.CodexArticleRead], budget_tokens: int
) -> list[mentor.CodexArticleRead]:
    """Статьи по порядку близости, пока помещаются в бюджет; длинная пропускается."""
    picked: list[mentor.CodexArticleRead] = []
    used = 0
    for article in articles:
        cost = estimate_tokens(article.full_text)
        if used + cost > budget_tokens:
            continue
        picked.append(article)
        used += cost
    return picked


def article_block(article: mentor.CodexArticleRead) -> str:
    """Статья в промпте: метка для ссылки, затем глава, заголовок, текст, источник."""
    return f"[Статья {article.number}] {article.full_text}"


def source_of(article: mentor.CodexArticleRead) -> MentorSource:
    return MentorSource(number=article.number, title=article.title, anchor=article.anchor)


async def cited_sources(answer: str) -> list[MentorSource]:
    """Статьи, на которые сослался ответ `[Статья N]`, в порядке первой ссылки.

    Ссылка на статью, которой в кодексе нет, отбрасывается (spec 0048 §5.3):
    модель вправе ошибиться номером, а ссылка в никуда хуже, чем никакой.
    """
    numbers = list(dict.fromkeys(_CITATION.findall(answer)))
    found = await mentor.get_articles(numbers)
    dropped = len(numbers) - len(found)
    if dropped:
        logger.warning("mentor_unknown_citation", dropped=dropped)
    return [source_of(found[number]) for number in numbers if number in found]


async def stream_reply(
    request: gateway.LlmRequest, providers: MentorProviders
) -> AsyncGenerator[MentorDelta | gateway.LlmResponse, None]:
    """Поток шлюза в куски для сотрудника; последним — весь ответ с учётом вызова.

    `aclosing` — канон шлюза: сотрудник ушёл посреди ответа — поток закрывается
    сразу, и шлюз пишет строку `aborted` со стоимостью резерва (ADR-024 §5).
    """
    async with aclosing(gateway.stream(request, provider=providers.streaming)) as events:
        async for event in events:
            if isinstance(event, gateway.LlmTextDelta):
                yield MentorDelta(text=event.text)
            else:
                yield event.response


def require_text(response: gateway.LlmResponse | None, *, flow: str) -> str:
    """Текст ответа модели; пустой ответ — ERR-AI-003: записывать и показывать нечего."""
    text = response.text.strip() if response is not None else ""
    if not text:
        logger.warning("mentor_empty_answer", flow=flow)
        raise AppError(
            code=gateway.ERR_AI_PROVIDER_ERROR,
            message="Model returned an empty answer",
            status_code=502,
        )
    return text
