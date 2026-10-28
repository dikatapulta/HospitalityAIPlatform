"""Загрузка кодекса (spec 0048 §4, §11): разбор до модели, векторы всех статей, замена."""

from __future__ import annotations

import uuid

import pytest

from hospitality.ai.gateway.api import EMBEDDING_DIMENSIONS, MockEmbeddingProvider, mock_embedding
from hospitality.ai.mentor.codex_upload import upload_codex
from hospitality.ai.mentor.common import MentorProviders
from hospitality.ai.mentor.tests.conftest import CODEX, codex_files
from hospitality.modules.mentor import api as mentor
from hospitality.shared.errors import AppError
from hospitality.shared.tenancy import tenant_context


def test_vector_column_matches_gateway_dimensions() -> None:
    """Модуль не импортирует шлюз (§5.1) — совпадение размерности держит этот тест."""
    assert mentor.ARTICLE_EMBEDDING_DIMENSIONS == EMBEDDING_DIMENSIONS


@pytest.mark.usefixtures("mentor_enabled")
async def test_upload_stores_a_vector_per_article(hotel: tuple[uuid.UUID, uuid.UUID]) -> None:
    tenant_id, user_id = hotel
    embeddings = MockEmbeddingProvider()
    with tenant_context(tenant_id):
        info = await upload_codex(
            codex_files(),
            uploaded_by_user_id=user_id,
            providers=MentorProviders(embeddings=embeddings),
        )
        assert (info.article_count, info.uploaded_by_user_id) == (3, user_id)
        [texts] = embeddings.calls  # одна пачка — один вызов шлюза
        assert len(texts) == 3
        assert await mentor.list_articles_needing_embedding("text-embedding-3-small") == []
        # Вектор каждой статьи — её собственный: поиск по нему находит её первой.
        for text in texts:
            [nearest] = await mentor.search_articles(mock_embedding(text), limit=1)
            assert nearest.full_text == text


@pytest.mark.usefixtures("mentor_enabled")
async def test_markup_error_costs_nothing_and_keeps_old_codex(
    hotel: tuple[uuid.UUID, uuid.UUID],
) -> None:
    tenant_id, _ = hotel
    embeddings = MockEmbeddingProvider()
    fakes = MentorProviders(embeddings=embeddings)
    with tenant_context(tenant_id):
        await upload_codex(codex_files(), uploaded_by_user_id=None, providers=fakes)
        broken = codex_files(CODEX.replace("#### Статья 3.", "#### Статья 5."))
        with pytest.raises(AppError) as error:
            await upload_codex(broken, uploaded_by_user_id=None, providers=fakes)
        assert error.value.code == mentor.ERR_MENTOR_CODEX_MARKUP
        assert error.value.message == "Файл 10_kodeks.md, строка 14: статья 5 вместо 3"
        info = await mentor.get_codex()
    assert len(embeddings.calls) == 1  # второй загрузке модель не понадобилась
    assert info is not None and info.article_count == 3
