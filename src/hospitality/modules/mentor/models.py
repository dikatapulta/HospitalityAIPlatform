"""ORM-модели модуля mentor (spec 0048 §4, ADR-023; идеи — spec 0071 §8).

Все семь таблиц тенантные: канон RLS скопирован с `TenantIsolationCanary`
(`platform/models.py`), RLS-блок — в миграциях 0043, 0058 и 0063 (копия канона 0002).
Исходник кодекса — загруженные файлы (`MentorCodex.files`); статьи — производный
индекс поиска, пересобирается каждой загрузкой.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from hospitality.shared.db import Base, UTCDateTime, utc_now
from hospitality.shared.tenancy import current_tenant_id

# Размерность вектора входит в схему (ADR-024 §3): смена модели эмбеддингов —
# миграция плюс пересчёт статей. То же число — `EMBEDDING_DIMENSIONS` шлюза;
# модуль шлюз не импортирует (§5.1), совпадение проверяет тест модуля.
ARTICLE_EMBEDDING_DIMENSIONS = 1536


class ConversationMode(enum.StrEnum):
    """Режим диалога: у сотрудника по одному открытому диалогу на режим (spec 0048 §4).

    `idea` и `interview` — «Предложить идею» (spec 0071): отправленная карточка
    закрывает диалог, следующая идея начинает новый. `ask` и `quiz` не закрываются.
    """

    ASK = "ask"
    QUIZ = "quiz"
    IDEA = "idea"
    INTERVIEW = "interview"


class MessageRole(enum.StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class IdeaKind(enum.StrEnum):
    """Карточка «Предложить идею» (spec 0071 §3–4): идея или итог интервью."""

    IDEA = "idea"
    INTERVIEW = "interview"


class IdeaMark(enum.StrEnum):
    """Пометка ИИ к идее — только основателю, автор её не видит (spec 0071 §6)."""

    EXISTS = "exists"
    SIMILAR = "similar"
    FEASIBLE = "feasible"
    CONSTRAINT = "constraint"
    UNKNOWN = "unknown"


class QuizGrade(enum.StrEnum):
    """Оценка ответа в «Тренировке» — первая строка проверки моделью (spec 0048 §5)."""

    CORRECT = "correct"
    PARTIAL = "partial"
    WRONG = "wrong"


def _string_enum(enum_class: type[enum.StrEnum], name: str) -> Enum:
    """Канон `request_status_column_type`: VARCHAR со значениями `.value`."""
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        length=16,
        values_callable=lambda members: [member.value for member in members],
    )


class MentorCodex(Base):
    """Кодекс отеля: загруженные файлы целиком, один на тенанта."""

    __tablename__ = "mentor_codexes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), unique=True, default=current_tenant_id
    )
    # Массив `{name, text}`: порядок файлов держит массив, а не ключи объекта (JSONB
    # переупорядочивает ключи). Разбор кодекса читает файлы в этом порядке.
    files: Mapped[list[dict[str, str]]] = mapped_column(JSONB())
    meta: Mapped[dict[str, str]] = mapped_column(JSONB())
    sha256: Mapped[str] = mapped_column(String(64))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    uploaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class MentorCodexArticle(Base):
    """Статья кодекса — строка индекса поиска с вектором."""

    __tablename__ = "mentor_codex_articles"
    # Имя явно: правило имён `uq_<таблица>_<колонка_0>` (shared/db.py) у составного
    # ключа взяло бы только первую колонку, и `alembic check` увидел бы расхождение.
    __table_args__ = (
        UniqueConstraint("tenant_id", "number", name="uq_mentor_codex_articles_tenant_id_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    number: Mapped[str] = mapped_column(String(16))  # «14» или «14-1»
    title: Mapped[str] = mapped_column(Text())
    chapter_number: Mapped[int] = mapped_column(Integer())
    chapter_title: Mapped[str] = mapped_column(Text())
    part_number: Mapped[int] = mapped_column(Integer())
    # Порядок статьи в кодексе: номер «14-1» строкой не сортируется.
    position: Mapped[int] = mapped_column(Integer())
    body: Mapped[str] = mapped_column(Text())
    source: Mapped[str] = mapped_column(Text())
    # NULL — вектор ещё не посчитан (кодекс засеян без модели) или посчитан другой
    # моделью: `ai/mentor` досчитывает такие статьи перед поиском.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(ARTICLE_EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str | None] = mapped_column(String(64))


class MentorConversation(Base):
    """Диалог сотрудника с наставником: один открытый на `(user_id, mode)`."""

    __tablename__ = "mentor_conversations"
    # Частичный индекс (миграция 0063, spec 0071 §8): закрытых диалогов идей у
    # сотрудника сколько угодно, открытый — один; `ask` и `quiz` не закрываются.
    __table_args__ = (
        Index(
            "uq_mentor_conversations_open_per_mode",
            "tenant_id",
            "user_id",
            "mode",
            unique=True,
            postgresql_where="closed_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    mode: Mapped[ConversationMode] = mapped_column(
        _string_enum(ConversationMode, "mentor_conversation_mode")
    )
    # Резюме старой части разговора (пишет модель). Может назвать гостя или коллегу —
    # PII_REGISTRY; ретеншн обнуляет резюме по его собственному возрасту.
    summary: Mapped[str | None] = mapped_column(Text())
    summary_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    # Текущий вопрос «Тренировки»: статья и текст вопроса до проверки ответа.
    quiz_article_number: Mapped[str | None] = mapped_column(String(16))
    quiz_question: Mapped[str | None] = mapped_column(Text())
    # Номера статей последних вопросов — их тренировка не повторяет (массив, свежие в конце).
    quiz_recent_articles: Mapped[list[str]] = mapped_column(
        JSONB(), default=list, server_default=sql_text("'[]'::jsonb")
    )
    # Момент отправки карточки идеи: диалог закрыт, следующая идея — новый диалог.
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)


class MentorMessage(Base):
    """Реплика диалога: вопрос сотрудника или ответ наставника."""

    __tablename__ = "mentor_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mentor_conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MessageRole] = mapped_column(_string_enum(MessageRole, "mentor_message_role"))
    text: Mapped[str] = mapped_column(Text())
    # Номера статей, на которые сослался ответ (`[Статья 30]`), — для ссылок под ответом.
    cited_articles: Mapped[list[str]] = mapped_column(
        JSONB(), default=list, server_default=sql_text("'[]'::jsonb")
    )
    # Реплика уже пересказана в `summary`: в историю для модели она не идёт, но
    # сотрудник видит её в своём диалоге до срока ретеншна.
    in_summary: Mapped[bool] = mapped_column(Boolean(), default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, index=True)


class MentorQuizAnswer(Base):
    """Оценка одного ответа «Тренировки»: поимённо, без текста ответа (решение 23.10)."""

    __tablename__ = "mentor_quiz_answers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    article_number: Mapped[str] = mapped_column(String(16))
    # Снимок главы на момент ответа: следующая загрузка кодекса может её переименовать.
    chapter_number: Mapped[int] = mapped_column(Integer())
    chapter_title: Mapped[str] = mapped_column(Text())
    grade: Mapped[QuizGrade] = mapped_column(_string_enum(QuizGrade, "mentor_quiz_grade"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)


class MentorExamAttempt(Base):
    """Попытка экзамена (spec 0048 §12, миграция 0058): ход, счёт и итог поимённо.

    Текста ответов и проверок в строке нет — только счёт; открытый вопрос хранится,
    пока на него не ответили. Число вопросов и порог — снимок настроек отеля на
    момент начала: смена порога не переписывает прошлые итоги. `finished_at` пуст —
    попытка идёт или брошена (начата новая); в итоги идут только законченные.
    """

    __tablename__ = "mentor_exam_attempts"
    __table_args__ = (
        CheckConstraint(
            "(finished_at IS NULL) = (percent IS NULL)"
            " AND (finished_at IS NULL) = (passed IS NULL)",
            name="result_only_when_finished",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # Тема: глава кодекса (снимок названия) или весь кодекс (`NULL`).
    chapter_number: Mapped[int | None] = mapped_column(Integer())
    chapter_title: Mapped[str | None] = mapped_column(Text())
    question_count: Mapped[int] = mapped_column(Integer())
    pass_percent: Mapped[int] = mapped_column(Integer())
    correct_count: Mapped[int] = mapped_column(Integer(), default=0, server_default=sql_text("0"))
    partial_count: Mapped[int] = mapped_column(Integer(), default=0, server_default=sql_text("0"))
    wrong_count: Mapped[int] = mapped_column(Integer(), default=0, server_default=sql_text("0"))
    # Ответы, оценку которых не удалось разобрать: не засчитаны, вместо них — другой вопрос.
    ungraded_count: Mapped[int] = mapped_column(Integer(), default=0, server_default=sql_text("0"))
    # Номера статей заданных вопросов по порядку; длина — номер хода для проверки ответа.
    asked_articles: Mapped[list[str]] = mapped_column(
        JSONB(), default=list, server_default=sql_text("'[]'::jsonb")
    )
    current_article_number: Mapped[str | None] = mapped_column(String(16))
    current_question: Mapped[str | None] = mapped_column(Text())
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    percent: Mapped[int | None] = mapped_column(Integer())
    passed: Mapped[bool | None] = mapped_column(Boolean())


class MentorIdea(Base):
    """Карточка идеи или итог интервью (spec 0071 §8, миграция 0063).

    Одна на диалог: пока `sent_at` пуст — черновик, его заменяет каждое новое
    предложение модели; отправка пишет пересказ из поля автора и закрывает диалог.
    Пометка — только у идеи: интервью выслушивают, а не оценивают (§4).
    """

    __tablename__ = "mentor_ideas"
    __table_args__ = (
        CheckConstraint(
            "kind = 'idea' OR (mark IS NULL AND mark_note IS NULL)",
            name="interview_has_no_mark",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, default=current_tenant_id
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mentor_conversations.id", ondelete="CASCADE"), unique=True
    )
    kind: Mapped[IdeaKind] = mapped_column(_string_enum(IdeaKind, "mentor_idea_kind"))
    # Пересказ на языке автора — может назвать коллегу (PII_REGISTRY).
    summary: Mapped[str] = mapped_column(Text())
    mark: Mapped[IdeaMark | None] = mapped_column(_string_enum(IdeaMark, "mentor_idea_mark"))
    mark_note: Mapped[str | None] = mapped_column(Text())
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, onupdate=utc_now)
