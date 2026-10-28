"""Загрузка кодекса: разбор → эмбеддинги всех статей → замена одной транзакцией
(spec 0048 §4).

Разбор — до вызова модели: файл с ошибкой разметки не стоит ни цента, а старый
кодекс остаётся (ERR-MENTOR-001 с файлом и строкой). Эмбеддинги считаются
синхронно в запросе: 200–300 статей — одна пачка и несколько секунд. Сбой модели
(ERR-AI-*) тоже оставляет старый кодекс: замена идёт только с готовыми векторами.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from hospitality.ai.gateway import api as gateway
from hospitality.ai.mentor.common import MentorProviders
from hospitality.modules.mentor import api as mentor
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


async def upload_codex(
    files: Sequence[mentor.CodexFile],
    *,
    uploaded_by_user_id: uuid.UUID | None,
    providers: MentorProviders | None = None,
) -> mentor.CodexInfo:
    """Заменить кодекс тенанта загруженными файлами. Внутри `tenant_context` (P-4)."""
    providers = providers or MentorProviders()
    drafts = mentor.parse_codex_files(files)
    response = await gateway.embed(
        [draft.full_text for draft in drafts], purpose="mentor", provider=providers.embeddings
    )
    info = await mentor.replace_codex(
        files,
        embeddings=response.vectors,
        embedding_model=response.model,
        uploaded_by_user_id=uploaded_by_user_id,
    )
    logger.info(
        "mentor_codex_uploaded", articles=info.article_count, embed_call_id=str(response.call_id)
    )
    return info
