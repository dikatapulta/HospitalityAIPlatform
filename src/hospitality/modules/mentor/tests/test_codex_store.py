"""Кодекс и индекс статей (spec 0048 §4): замена целиком, поиск по вектору, RLS."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from hospitality.modules.mentor.api import (
    ERR_MENTOR_CODEX_MARKUP,
    CodexFile,
    get_articles,
    get_codex,
    list_articles_needing_embedding,
    load_codex_document,
    parse_codex_files,
    pick_quiz_article,
    replace_codex,
    search_articles,
    store_article_embeddings,
)
from hospitality.modules.mentor.models import MentorCodexArticle
from hospitality.modules.mentor.tests.conftest import BODY, codex_files, make_user, unit_vector
from hospitality.shared.db import session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context

MODEL = "text-embedding-3-small"


async def _replace_with_vectors(files: list[CodexFile] | None = None) -> None:
    files = files or codex_files()
    count = len(parse_codex_files(files))
    await replace_codex(
        files,
        embeddings=[unit_vector(i) for i in range(count)],
        embedding_model=MODEL,
        uploaded_by_user_id=None,
    )


async def test_replace_stores_files_articles_and_info(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    manager = await make_user("Менеджер")
    with tenant_context(tenant_a):
        info = await replace_codex(
            codex_files(), embeddings=None, embedding_model=None, uploaded_by_user_id=manager
        )
        assert info.title == "Кодекс отеля «Тест»"
        assert info.file_names == ["00_head.md", "10_body.md"]  # порядок — по имени
        assert info.article_count == 4
        assert info.uploaded_by_user_id == manager
        assert await get_codex() == info

        document = await load_codex_document()
        assert document is not None
        assert [a.number for a in document.articles] == ["1", "2", "2-1", "3"]

        articles = await get_articles(["2-1", "3", "99"])
        assert sorted(articles) == ["2-1", "3"]  # несуществующий номер не попал
        assert articles["3"].chapter_title == "Портье"
        assert articles["3"].position == 4
        assert articles["3"].full_text.startswith("Глава 3. Портье\nСтатья 3. Третья\n1. ")
        assert articles["3"].full_text.endswith("Источник: стандарт портье.")


async def test_markup_error_keeps_old_codex(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        await _replace_with_vectors()
        broken = codex_files(BODY.replace("### Глава 3.", "### Глава 4."))
        with pytest.raises(AppError) as error:
            await replace_codex(
                broken, embeddings=None, embedding_model=None, uploaded_by_user_id=None
            )
        assert error.value.code == ERR_MENTOR_CODEX_MARKUP
        assert error.value.status_code == 422
        assert error.value.message.startswith("Файл 10_body.md, строка ")
        assert "глава 4 вместо 3" in error.value.message

        info = await get_codex()
        assert info is not None and info.article_count == 4
        assert await list_articles_needing_embedding(MODEL) == []


async def test_new_upload_replaces_articles_entirely(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        await _replace_with_vectors()
        shorter = BODY.split("### Глава 3.")[0]
        await replace_codex(
            codex_files(shorter), embeddings=None, embedding_model=None, uploaded_by_user_id=None
        )
        info = await get_codex()
        assert info is not None and info.article_count == 3
        assert await get_articles(["3"]) == {}


async def test_vectors_must_match_articles() -> None:
    with pytest.raises(ValueError, match="векторов"):
        await replace_codex(
            codex_files(),
            embeddings=[unit_vector(0)],
            embedding_model=MODEL,
            uploaded_by_user_id=None,
        )
    with pytest.raises(ValueError, match="вместе"):
        await replace_codex(
            codex_files(), embeddings=None, embedding_model=MODEL, uploaded_by_user_id=None
        )


async def test_search_returns_nearest_articles(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        await _replace_with_vectors()
        hits = await search_articles(unit_vector(2), limit=2)
        assert [hit.number for hit in hits][0] == "2-1"
        assert len(hits) == 2


async def test_missing_and_stale_vectors_are_listed_and_filled(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        await replace_codex(
            codex_files(), embeddings=None, embedding_model=None, uploaded_by_user_id=None
        )
        pending = await list_articles_needing_embedding(MODEL)
        assert [a.number for a in pending] == ["1", "2", "2-1", "3"]
        assert await search_articles(unit_vector(0), limit=10) == []  # без векторов не ищется

        await store_article_embeddings(
            {article.id: unit_vector(i) for i, article in enumerate(pending)}, model=MODEL
        )
        assert await list_articles_needing_embedding(MODEL) == []
        # Другая модель эмбеддингов — все векторы устарели.
        assert len(await list_articles_needing_embedding("text-embedding-3-large")) == 4


async def test_quiz_article_respects_length_chapter_recent_and_topic(
    two_tenants: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_a, _ = two_tenants
    with tenant_context(tenant_a):
        await _replace_with_vectors()
        # Тренировочные — только длинные статьи 2-1 и 3.
        picked = {
            (await pick_quiz_article(chapter_number=None, near=None, exclude=[])).number  # type: ignore[union-attr]
            for _ in range(20)
        }
        assert picked == {"2-1", "3"}

        by_chapter = await pick_quiz_article(chapter_number=3, near=None, exclude=[])
        assert by_chapter is not None and by_chapter.number == "3"
        # В главе 1 длинных статей нет — вопрос по короткой лучше отказа.
        short = await pick_quiz_article(chapter_number=1, near=None, exclude=[])
        assert short is not None and short.number == "1"
        assert await pick_quiz_article(chapter_number=9, near=None, exclude=[]) is None

        fresh = await pick_quiz_article(chapter_number=None, near=None, exclude=["3"])
        assert fresh is not None and fresh.number == "2-1"
        # Спрошены все — повтор лучше, чем пустой ответ.
        again = await pick_quiz_article(chapter_number=None, near=None, exclude=["2-1", "3"])
        assert again is not None

        near = await pick_quiz_article(chapter_number=None, near=unit_vector(3), exclude=[])
        assert near is not None and near.number == "3"
        next_near = await pick_quiz_article(chapter_number=None, near=unit_vector(3), exclude=["3"])
        assert next_near is not None and next_near.number == "2-1"


async def test_other_tenant_sees_no_codex(two_tenants: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_a, tenant_b = two_tenants
    with tenant_context(tenant_a):
        await _replace_with_vectors()
    with tenant_context(tenant_b):
        assert await get_codex() is None
        assert await load_codex_document() is None
        assert await search_articles(unit_vector(0), limit=10) == []
        assert await get_articles(["1"]) == {}
        async with session_scope() as session:
            assert list(await session.scalars(select(MentorCodexArticle))) == []
        # Свой кодекс у B — независим от кодекса A.
        await _replace_with_vectors(codex_files(BODY.split("### Глава 3.")[0]))
        info = await get_codex()
        assert info is not None and info.article_count == 3
    with tenant_context(tenant_a):
        info = await get_codex()
        assert info is not None and info.article_count == 4
