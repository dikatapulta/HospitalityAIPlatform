"""Схемы модуля memos на границе (R-6): ввод черновика и действий, чтение записи.

Тексты в схемах чтения — расшифрованные: схему отдают только участнику записи
(§5), в логи она не идёт.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Annotated, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from hospitality.modules.memos.models import (
    MemoKind,
    MemoStatus,
    SignatureResponse,
    SignatureRole,
)
from hospitality.platform.staff_confirm import ConfirmationMethod, ProofStatus

TITLE_MAX_CHARS: Final = 200
BODY_MAX_CHARS: Final = 6000
REMARK_MAX_CHARS: Final = 1000
COMMENT_MAX_CHARS: Final = 1000
MAX_SIGNERS: Final = 10
# Образец подписи: штрихи в квадрате 0…1000; больше — не рука, а мусор.
MAX_STROKES: Final = 64
MAX_STROKE_POINTS: Final = 4000

_Title = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TITLE_MAX_CHARS)
]
_Body = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=BODY_MAX_CHARS)
]
_Remark = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=REMARK_MAX_CHARS)
]
_Comment = Annotated[str, StringConstraints(strip_whitespace=True, max_length=COMMENT_MAX_CHARS)]


class MemoDraftInput(BaseModel):
    """Черновик записки (§3.1): без подписантов — можно, адресат — один."""

    model_config = ConfigDict(frozen=True)

    template_key: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,39}$")
    title: _Title
    body: _Body
    addressee_id: uuid.UUID
    signer_ids: tuple[uuid.UUID, ...] = Field(default=(), max_length=MAX_SIGNERS)

    @field_validator("signer_ids")
    @classmethod
    def _signers_unique(cls, value: tuple[uuid.UUID, ...]) -> tuple[uuid.UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("signer listed twice")
        return value


class MemoDecision(enum.StrEnum):
    APPROVED = MemoStatus.APPROVED.value
    REJECTED = MemoStatus.REJECTED.value
    RETURNED = MemoStatus.RETURNED.value


class SendIntent(BaseModel):
    """Автор: «Отправить на подпись» или сразу руководителю, если подписантов нет."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["send"] = "send"


class SignIntent(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["sign"] = "sign"


class RemarkIntent(BaseModel):
    """«Есть замечание» — подпись с текстом замечания."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["remark"] = "remark"
    remark: _Remark


class DecisionIntent(BaseModel):
    """Решение адресата; комментарий обязателен у «Отказано» и «На доработку» (§3.5)."""

    model_config = ConfigDict(frozen=True)
    kind: Literal["decide"] = "decide"
    decision: MemoDecision
    comment: _Comment = ""

    @model_validator(mode="after")
    def _comment_required_unless_approved(self) -> Self:
        if self.decision is not MemoDecision.APPROVED and not self.comment:
            raise ValueError("comment is required to reject or return")
        return self


MemoIntent = Annotated[
    SendIntent | SignIntent | RemarkIntent | DecisionIntent, Field(discriminator="kind")
]


class PasskeyAnswer(BaseModel):
    """Ответ ключа устройства на окно подтверждения."""

    model_config = ConfigDict(frozen=True)
    challenge_id: str = Field(min_length=1, max_length=128)
    credential_json: str


class PasswordAnswer(BaseModel):
    """Пароль кабинета в том же окне; IP и токен устройства — бюджеты входа."""

    model_config = ConfigDict(frozen=True)
    challenge_id: str = Field(min_length=1, max_length=128)
    password: str
    client_ip: str
    device_token: str | None = None


class SignatureStrokes(BaseModel):
    """Образец подписи: штрихи пальцем, точки `[x, y]` в квадрате 0…1000."""

    model_config = ConfigDict(frozen=True)

    strokes: tuple[tuple[tuple[int, int], ...], ...] = Field(min_length=1, max_length=MAX_STROKES)

    @field_validator("strokes")
    @classmethod
    def _strokes_are_drawable(
        cls, value: tuple[tuple[tuple[int, int], ...], ...]
    ) -> tuple[tuple[tuple[int, int], ...], ...]:
        if sum(len(stroke) for stroke in value) > MAX_STROKE_POINTS:
            raise ValueError(f"signature has more than {MAX_STROKE_POINTS} points")
        for stroke in value:
            if not stroke:
                raise ValueError("empty stroke")
            if any(not (0 <= x <= 1000 and 0 <= y <= 1000) for x, y in stroke):
                raise ValueError("point outside 0..1000")
        return value


class SignatureSampleRead(BaseModel):
    id: uuid.UUID
    strokes: SignatureStrokes
    created_at: datetime


class SignatureRead(BaseModel):
    """Строка листа подписей (§5). `response` None после отправки адресату —
    «не ответил к моменту отправки»; до неё — ещё ждут."""

    user_id: uuid.UUID
    role: SignatureRole
    response: SignatureResponse | None
    remark: str | None
    method: ConfirmationMethod | None
    signed_at: datetime | None
    sample: SignatureSampleRead | None
    # Сходится ли доказательство с нынешним содержимым; None — подтверждения ещё нет.
    proof_status: ProofStatus | None
    # Подпись паролем: когда этот пароль задан по ссылке нового пароля (§4).
    password_reset_at: datetime | None = None
    # Подпись ключом: ключ потом погашен новым паролем по ссылке — когда.
    key_reset_revoked_at: datetime | None = None
    # Подпись ключом: последний сброс пароля по ссылке до привязки ключа.
    key_bound_after_reset_at: datetime | None = None


class ViewRead(BaseModel):
    user_id: uuid.UUID
    viewed_at: datetime


class MemoSummary(BaseModel):
    """Строка реестра (§5): без текста и листа."""

    id: uuid.UUID
    kind: MemoKind
    number: str | None
    status: MemoStatus
    title: str
    author_id: uuid.UUID
    addressee_id: uuid.UUID
    created_at: datetime
    sent_at: datetime | None
    updated_at: datetime


class MemoCard(MemoSummary):
    """Карточка (§5): текст, лист подписей, решение, просмотры."""

    template_key: str | None
    body: str
    based_on_id: uuid.UUID | None
    # Номер записки, взамен которой эта («взамен СЗ-2026/14», §3.6).
    based_on_number: str | None = None
    submitted_at: datetime | None
    decided_at: datetime | None
    decision_comment: str | None
    signatures: list[SignatureRead]
    views: list[ViewRead]
    # Текст в базе тот же, что был при отправке (хэш на момент отправки сходится).
    content_intact: bool


class MemoParticipants(BaseModel):
    """Участники записи без текста — кому слать пуш о шаге (§3)."""

    id: uuid.UUID
    status: MemoStatus
    author_id: uuid.UUID
    addressee_id: uuid.UUID
    signer_ids: list[uuid.UUID]


class MemoBox(enum.StrEnum):
    """Вкладки реестра (§5)."""

    WAITING = "waiting"  # «Ждут меня»: подписать или решить
    MINE = "mine"  # «Мои»: я автор
    ALL = "all"  # «Все»: я участник
