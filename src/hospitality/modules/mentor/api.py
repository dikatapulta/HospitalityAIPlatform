"""Публичный интерфейс модуля mentor (spec 0048, FOUNDATION §5.2, R-5) — копия канона
`modules/requests/api.py`.

Единственная точка входа в модуль снаружи: композиция `ai/mentor`, кабинет персонала,
сид и джоба ретеншна импортируют ТОЛЬКО отсюда — остальные файлы модуля приватны
(контракт 5 import-linter). Здесь нет логики, только контракт.
"""

from __future__ import annotations

from hospitality.modules.mentor.codex import (
    MAX_ARTICLE_CHARS,
    Codex,
    CodexArticle,
    CodexChapter,
    CodexPart,
    CodexSection,
)
from hospitality.modules.mentor.dialogs import (
    ERR_MENTOR_NO_QUIZ_QUESTION,
    QUIZ_NO_REPEAT,
    active_quiz_question,
    append_messages,
    compact_history,
    delete_conversation,
    delete_expired_mentor_texts,
    finish_quiz_answer,
    get_conversation,
    start_quiz_question,
)
from hospitality.modules.mentor.models import (
    ARTICLE_EMBEDDING_DIMENSIONS,
    ConversationMode,
    MessageRole,
    QuizGrade,
)
from hospitality.modules.mentor.schemas import (
    CodexArticleDraft,
    CodexArticleRead,
    CodexFile,
    CodexInfo,
    ConversationRead,
    MentorMessageRead,
    MentorRetentionStats,
    NewMentorMessage,
)
from hospitality.modules.mentor.service import (
    ERR_MENTOR_CODEX_MARKUP,
    ERR_MENTOR_CODEX_MISSING,
    QUIZ_MIN_ARTICLE_CHARS,
    get_articles,
    get_codex,
    has_codex,
    list_articles_needing_embedding,
    load_codex_document,
    parse_codex_files,
    pick_quiz_article,
    replace_codex,
    search_articles,
    store_article_embeddings,
)

__all__ = [
    "ARTICLE_EMBEDDING_DIMENSIONS",
    "ERR_MENTOR_CODEX_MARKUP",
    "ERR_MENTOR_CODEX_MISSING",
    "ERR_MENTOR_NO_QUIZ_QUESTION",
    "MAX_ARTICLE_CHARS",
    "QUIZ_MIN_ARTICLE_CHARS",
    "QUIZ_NO_REPEAT",
    "Codex",
    "CodexArticle",
    "CodexArticleDraft",
    "CodexArticleRead",
    "CodexChapter",
    "CodexFile",
    "CodexInfo",
    "CodexPart",
    "CodexSection",
    "ConversationMode",
    "ConversationRead",
    "MentorMessageRead",
    "MentorRetentionStats",
    "MessageRole",
    "NewMentorMessage",
    "QuizGrade",
    "active_quiz_question",
    "append_messages",
    "compact_history",
    "delete_conversation",
    "delete_expired_mentor_texts",
    "finish_quiz_answer",
    "get_articles",
    "get_codex",
    "get_conversation",
    "has_codex",
    "list_articles_needing_embedding",
    "load_codex_document",
    "parse_codex_files",
    "pick_quiz_article",
    "replace_codex",
    "search_articles",
    "start_quiz_question",
    "store_article_embeddings",
]
