"""Pydantic-схемы границ модуля mentor (spec 0048 §4, spec 0071 §8, R-6)."""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.mentor.models import ConversationMode, IdeaKind, IdeaMark, MessageRole


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
    # Карточки «Предложить идею»: черновики старше срока реплик и отправленные
    # старше `IDEA_RETENTION_DAYS` (spec 0071 §8).
    ideas_deleted: int


class MentorActivity(BaseModel):
    """Польза «Наставника» за окно — числа «пульса пользы» (spec 0035 §8.1).

    `turns` — реплики сотрудников во всех режимах, `people` — сколько разных сотрудников
    их написали. Экзамены — личные и назначенные, зачётные и пробные: пульс про то,
    пользуются ли, а не про итоги. `ungraded` — не засчитанные ответы попыток, начатых в окне.
    """

    model_config = ConfigDict(frozen=True)

    turns: int
    people: int
    exams_started: int
    exams_finished: int
    exams_passed: int
    ungraded: int


class ExamAttemptRead(BaseModel):
    """Попытка экзамена (spec 0048 §12): снимок настроек, счёт, открытый вопрос, итог."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    user_id: uuid.UUID
    assignment_id: uuid.UUID | None
    chapter_number: int | None
    chapter_title: str | None
    question_count: int
    pass_percent: int
    on_draft_codex: bool
    correct_count: int
    partial_count: int
    wrong_count: int
    ungraded_count: int
    asked_articles: list[str]
    current_article_number: str | None
    current_question: str | None
    started_at: datetime
    finished_at: datetime | None
    percent: int | None
    passed: bool | None

    @property
    def answered(self) -> int:
        """Засчитанных ответов — тех, у которых разобрана оценка."""
        return self.correct_count + self.partial_count + self.wrong_count

    @property
    def turn(self) -> int:
        """Заданных вопросов — номер хода: ответ присылают с ним (вопрос сменился — отказ)."""
        return len(self.asked_articles)

    @property
    def finished(self) -> bool:
        return self.finished_at is not None

    @property
    def passes_assignment(self) -> bool:
        """Сдана и засчитывается в назначение: пробная (на проекте кодекса) — нет (spec 0072 §6)."""
        return bool(self.passed) and not self.on_draft_codex


class ArticleFlagCount(BaseModel):
    """«Неточность в статье» по статье (spec 0072 §6): сколько сотрудников отметили."""

    model_config = ConfigDict(frozen=True)

    article_number: str
    title: str
    count: int


class ExamChapterRef(BaseModel):
    """Глава темы назначения — снимок номера и названия (spec 0072 §3)."""

    model_config = ConfigDict(frozen=True)

    number: int
    title: str


class ExamAssignmentRead(BaseModel):
    """Назначенный экзамен (spec 0072 §3): `chapters` пусто — весь кодекс, `service_keys`
    пусто — всем; `archived_at` — снят."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    chapters: list[ExamChapterRef]
    service_keys: list[str]
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    archived_at: datetime | None

    def is_for(self, service_keys: Collection[str]) -> bool:
        """Назначен ли сотруднику с этими службами: «всем» или хотя бы одной из них."""
        return not self.service_keys or any(key in service_keys for key in self.service_keys)


class IdeaRead(BaseModel):
    """Карточка идеи или итог интервью (spec 0071 §8): `sent_at` пуст — черновик."""

    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    kind: IdeaKind
    summary: str
    mark: IdeaMark | None
    mark_note: str | None
    sent_at: datetime | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @property
    def sent(self) -> bool:
        return self.sent_at is not None


class SentIdeaRead(BaseModel):
    """Отправленная карточка с разговором — «Все идеи» и выгрузка (spec 0071 §8).

    Реплик нет — их срок истёк раньше карточки (90 дней против 365).
    """

    model_config = ConfigDict(frozen=True)

    idea: IdeaRead
    messages: list[MentorMessageRead]
