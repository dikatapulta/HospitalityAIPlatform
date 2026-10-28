"""Кодекс и индекс статей модуля mentor (spec 0048 §4, ADR-023) — копия канона
`modules/requests/service.py`.

Модуль о модели не знает: поиск принимает готовый вектор, загрузка — готовые
эмбеддинги. Каждая функция — одна транзакция внутри `tenant_context`; чужой кодекс
отсекает RLS (P-4).
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from hospitality.modules.mentor.codex import Codex, CodexMarkupError, parse_codex
from hospitality.modules.mentor.models import MentorCodex, MentorCodexArticle
from hospitality.modules.mentor.schemas import (
    CodexArticleDraft,
    CodexArticleRead,
    CodexFile,
    CodexInfo,
)
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_MENTOR_CODEX_MARKUP = "ERR-MENTOR-001"
ERR_MENTOR_CODEX_MISSING = "ERR-MENTOR-002"

# «Тренировка» спрашивает по статьям не короче этого (spec 0048 §5): в короткой
# статье нет ситуации, о которой можно задать практический вопрос.
QUIZ_MIN_ARTICLE_CHARS: Final = 250
# Тема свободным текстом: вопрос — по одной из ближайших статей (как в «Наставнике»).
_QUIZ_NEAR_CANDIDATES: Final = 3


def parse_codex_files(files: Sequence[CodexFile]) -> list[CodexArticleDraft]:
    """Статьи кодекса по порядку; ошибка разметки — ERR-MENTOR-001 с файлом и строкой."""
    return _drafts(_parse(files))


async def replace_codex(
    files: Sequence[CodexFile],
    *,
    embeddings: Sequence[list[float]] | None,
    embedding_model: str | None,
    uploaded_by_user_id: uuid.UUID | None,
) -> CodexInfo:
    """Заменить кодекс и его статьи целиком одной транзакцией (spec 0048 §4).

    Ошибка разметки — ERR-MENTOR-001 до записи: старый кодекс остаётся.
    `embeddings` — векторы статей в порядке `parse_codex_files`; `None` — кодекс
    без векторов (сид демо-тенанта): их досчитает `ai/mentor` перед поиском.
    Две загрузки разом упорядочивает блокировка строки кодекса (upsert): вторая
    ждёт первую и заменяет её статьи своими.
    """
    codex = _parse(files)
    drafts = _drafts(codex)
    if (embeddings is None) != (embedding_model is None):
        raise ValueError("replace_codex: embeddings и embedding_model задаются вместе")
    if embeddings is not None and len(embeddings) != len(drafts):
        raise ValueError(f"replace_codex: {len(embeddings)} векторов на {len(drafts)} статей")
    payload = [{"name": f.name, "text": f.text} for f in sorted(files, key=lambda f: f.name)]
    sha256 = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
    uploaded_at = utc_now()
    values = {
        "files": payload,
        "meta": codex.meta,
        "sha256": sha256,
        "uploaded_by_user_id": uploaded_by_user_id,
        "uploaded_at": uploaded_at,
    }
    async with session_scope() as session:
        await session.execute(
            insert(MentorCodex)
            .values(id=uuid.uuid4(), **values)
            .on_conflict_do_update(index_elements=["tenant_id"], set_=values)
        )
        await session.execute(delete(MentorCodexArticle))
        vectors: Sequence[list[float] | None] = embeddings or [None] * len(drafts)
        session.add_all(
            MentorCodexArticle(
                **draft.model_dump(), embedding=vector, embedding_model=embedding_model
            )
            for draft, vector in zip(drafts, vectors, strict=True)
        )
    logger.info(
        "mentor_codex_replaced",
        files=len(payload),
        articles=len(drafts),
        embedded=embeddings is not None,
    )
    return _info(
        codex,
        file_names=[f["name"] for f in payload],
        article_count=len(drafts),
        sha256=sha256,
        uploaded_at=uploaded_at,
        uploaded_by_user_id=uploaded_by_user_id,
    )


async def get_codex() -> CodexInfo | None:
    """Сведения о кодексе тенанта; `None` — кодекс не загружен."""
    async with session_scope() as session:
        row = await session.scalar(select(MentorCodex))
        if row is None:
            return None
        count = await session.scalar(select(func.count()).select_from(MentorCodexArticle))
    return _info(
        _parse([CodexFile(**f) for f in row.files]),
        file_names=[f["name"] for f in row.files],
        article_count=count or 0,
        sha256=row.sha256,
        uploaded_at=row.uploaded_at,
        uploaded_by_user_id=row.uploaded_by_user_id,
    )


async def load_codex_document() -> Codex | None:
    """Дерево кодекса для страницы «Кодекс» — разбором загруженных файлов."""
    async with session_scope() as session:
        files = await session.scalar(select(MentorCodex.files))
    return None if files is None else _parse([CodexFile(**f) for f in files])


async def list_articles_needing_embedding(model: str) -> list[CodexArticleRead]:
    """Статьи без вектора или с вектором другой модели — по порядку кодекса."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorCodexArticle)
            .where(
                (MentorCodexArticle.embedding_model.is_(None))
                | (MentorCodexArticle.embedding_model != model)
            )
            .order_by(MentorCodexArticle.position)
        )
        return [CodexArticleRead.model_validate(row) for row in rows]


async def store_article_embeddings(vectors: dict[uuid.UUID, list[float]], *, model: str) -> None:
    """Записать векторы статей. Статьи, заменённые новой загрузкой, тихо пропускаются."""
    async with session_scope() as session:
        for article_id, vector in vectors.items():
            await session.execute(
                update(MentorCodexArticle)
                .where(MentorCodexArticle.id == article_id)
                .values(embedding=vector, embedding_model=model)
            )
    logger.info("mentor_article_embeddings_stored", articles=len(vectors))


async def search_articles(vector: list[float], *, limit: int) -> list[CodexArticleRead]:
    """Статьи, ближайшие к вектору по косинусу; без вектора — не участвуют."""
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorCodexArticle)
            .where(MentorCodexArticle.embedding.is_not(None))
            .order_by(MentorCodexArticle.embedding.cosine_distance(vector))
            .limit(limit)
        )
        return [CodexArticleRead.model_validate(row) for row in rows]


async def get_articles(numbers: Sequence[str]) -> dict[str, CodexArticleRead]:
    """Статьи по номерам; несуществующие номера в ответ не попадают."""
    if not numbers:
        return {}
    async with session_scope() as session:
        rows = await session.scalars(
            select(MentorCodexArticle).where(MentorCodexArticle.number.in_(numbers))
        )
        return {row.number: CodexArticleRead.model_validate(row) for row in rows}


async def has_codex() -> bool:
    """Загружен ли кодекс — без разбора файлов (проверка перед каждым вопросом)."""
    async with session_scope() as session:
        found = await session.scalar(select(MentorCodex.id).limit(1))
    return found is not None


async def pick_quiz_article(
    *, chapter_number: int | None, near: list[float] | None, exclude: Sequence[str]
) -> CodexArticleRead | None:
    """Статья для вопроса «Тренировки» (spec 0048 §5); `None` — в главе нет статей.

    Подходят статьи от `QUIZ_MIN_ARTICLE_CHARS` знаков, по главе, если она задана;
    длинных нет вовсе — любые (как в «Наставнике»: вопрос лучше, чем отказ). С
    вектором темы — ближайшая из трёх ближайших, которую не спрашивали недавно
    (`exclude`), иначе самая близкая. Без темы — случайная из не спрошенных недавно,
    а если спрошены все — случайная из всех.
    """
    rows: list[CodexArticleRead] = []
    for min_chars in (QUIZ_MIN_ARTICLE_CHARS, 0):
        rows = await _quiz_candidates(chapter_number=chapter_number, near=near, min_chars=min_chars)
        if rows:
            break
    if not rows:
        return None
    fresh = [row for row in rows if row.number not in exclude]
    if near is not None:
        return (fresh or rows)[0]
    return random.choice(fresh or rows)  # noqa: S311 — выбор вопроса, не криптография


async def _quiz_candidates(
    *, chapter_number: int | None, near: list[float] | None, min_chars: int
) -> list[CodexArticleRead]:
    query = select(MentorCodexArticle).where(func.length(MentorCodexArticle.body) >= min_chars)
    if chapter_number is not None:
        query = query.where(MentorCodexArticle.chapter_number == chapter_number)
    if near is not None:
        query = (
            query.where(MentorCodexArticle.embedding.is_not(None))
            .order_by(MentorCodexArticle.embedding.cosine_distance(near))
            .limit(_QUIZ_NEAR_CANDIDATES)
        )
    async with session_scope() as session:
        return [CodexArticleRead.model_validate(row) for row in await session.scalars(query)]


def _parse(files: Sequence[CodexFile]) -> Codex:
    try:
        return parse_codex([(f.name, f.text) for f in files])
    except CodexMarkupError as error:
        # WARNING, а не ERROR: это ошибка в файле менеджера, а не сбой (и ERROR унёс
        # бы тело запроса в Sentry до #433 — LATER, ревью PR #499 Н-3).
        logger.warning(
            "mentor_codex_markup_error",
            error_code=ERR_MENTOR_CODEX_MARKUP,
            file_name=error.file_name,
            line=error.line,
        )
        raise AppError(code=ERR_MENTOR_CODEX_MARKUP, message=str(error), status_code=422) from error


def _drafts(codex: Codex) -> list[CodexArticleDraft]:
    return [
        CodexArticleDraft(
            number=article.number,
            title=article.title,
            chapter_number=chapter.number,
            chapter_title=chapter.title,
            part_number=part.number,
            position=position,
            body=article.body,
            source=article.source,
        )
        for position, (part, chapter, article) in enumerate(codex.walk(), 1)
    ]


def _info(
    codex: Codex,
    *,
    file_names: list[str],
    article_count: int,
    sha256: str,
    uploaded_at: datetime,
    uploaded_by_user_id: uuid.UUID | None,
) -> CodexInfo:
    return CodexInfo(
        title=codex.meta["Название"],
        meta=codex.meta,
        file_names=file_names,
        article_count=article_count,
        sha256=sha256,
        uploaded_at=uploaded_at,
        uploaded_by_user_id=uploaded_by_user_id,
    )
