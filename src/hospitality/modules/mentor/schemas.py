"""Pydantic-схемы границ модуля mentor (spec 0048 §4, R-6)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.mentor.models import ConversationMode, MessageRole


class CodexFile(BaseModel):
    """Один загруженный файл кодекса: имя и текст разметки."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=255)
    text: str


class CodexArticleDraft(BaseModel):
    """Статья после разбора, до записи: из неё строится текст эмбеддинга."""

    model_config = ConfigDict(frozen=True, from_attributes=True)

    number: str
    title: str
    chapter_number: int
    chapter_title: str
    part_number: int
    position: int
    body: str
    source: str

    @property
    def anchor(self) -> str:
        return f"st-{self.number}"

    @property
    def heading(self) -> str:
        return f"Статья {self.number}. {self.title}"

    @property
    def full_text(self) -> str:
        """Статья целиком, как её видит модель: глава, заголовок, текст, источник.

        Тот же текст уходит в эмбеддинг — поиск и промпт видят статью одинаково.
        """
        return (
            f"Глава {self.chapter_number}. {self.chapter_title}\n"
            f"{self.heading}\n{self.body}\nИсточник: {self.source}"
        )


class CodexArticleRead(CodexArticleDraft):
    """Статья индекса поиска."""

    id: uuid.UUID


class CodexInfo(BaseModel):
    """Сведения о загруженном кодексе — без текстов файлов."""

    model_config = ConfigDict(frozen=True)

    title: str
    meta: dict[str, str]
    file_names: list[str]
    article_count: int
    sha256: str
    uploaded_at: datetime
    uploaded_by_user_id: uuid.UUID | None


class MentorMessageRead(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)

    id: uuid.UUID
    role: MessageRole
    text: str
    cited_articles: list[str]
    in_summary: bool
    created_at: datetime


class NewMentorMessage(BaseModel):
    """Реплика к записи: время задаёт вызывающий — вопрос раньше ответа на поток."""

    model_config = ConfigDict(frozen=True)

    role: MessageRole
    text: str = Field(min_length=1)
    cited_articles: list[str] = Field(default_factory=list)
    created_at: datetime


class ConversationRead(BaseModel):
    """Диалог сотрудника в одном режиме со всеми репликами по порядку."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    mode: ConversationMode
    summary: str | None
    quiz_article_number: str | None
    quiz_question: str | None
    quiz_recent_articles: list[str]
    messages: list[MentorMessageRead]

    @property
    def history(self) -> list[MentorMessageRead]:
        """Реплики, ещё не пересказанные в резюме, — то, что модель видит дословно."""
        return [message for message in self.messages if not message.in_summary]


class QuizCounts(BaseModel):
    """Оценки «Тренировки» одной строки статистики «Где ошибаются» (spec 0048 §6)."""

    model_config = ConfigDict(frozen=True)

    correct: int
    partial: int
    wrong: int

    @property
    def total(self) -> int:
        return self.correct + self.partial + self.wrong

    @property
    def errors(self) -> int:
        """«Ошибок» — неверные и частично верные вместе (как в «Наставнике»)."""
        return self.partial + self.wrong


class ChapterQuizStats(QuizCounts):
    """Глава — снимком на момент ответа: загрузка кодекса может её переименовать."""

    chapter_number: int
    chapter_title: str


class UserQuizStats(QuizCounts):
    user_id: uuid.UUID


class MentorRetentionStats(BaseModel):
    """Счётчики одного прохода ретеншна модуля (spec 0048 §4)."""

    model_config = ConfigDict(frozen=True)

    messages_deleted: int
    summaries_cleared: int
