"""Действия «Наставника» в кабинете: поток ответа, «Начать заново», загрузка кодекса,
«Обнулить статистику» (spec 0048 §5–7; issue #492, PR 4 серии).

Канон `catalog_api.py`: CSRF-щит `browser.require_json_same_origin`, личность —
`staff_auth.require_role`, раздел выключен у отеля — 404. Ответ модели идёт
потоком `text/event-stream` (§5.1): события `delta` {text}…, затем `done`
{answer, sources} или `error` {code, message}. Ошибка хода (кодекс не загружен,
лимит дня, модель не отвечает, вопрос «Тренировки» уже проверен) — событие
`error` с текстом сотруднику (§7, `mentor.error_text`), неожиданная — тоже `error`,
«Наставник сейчас не отвечает»; пустой, слишком длинный или с NUL текст — 422 до
потока (ревью PR #512, Н-11; #531).

Ушёл сотрудник посреди ответа — ход доводит текущую фазу и закрывается на
ближайшем куске (`MentorStreamResponse`, #531): посреди ответа модели шлюз пишет
вызов `aborted` со стоимостью резерва, и диалог не меняется (ADR-024 §5); ответ,
показанный целиком, записывается.
`user_id` в логах этих маршрутов не пишется: по `correlation_id` он связал бы
оценку «Тренировки» с сотрудником (ревью PR #512, Н-17; PII_REGISTRY).
Скрипт страницы — `static/mentor_chat.js`, кодекса — `static/mentor_codex.js`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from typing import Annotated, Final

import anyio
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from hospitality.ai.mentor import ask as mentor_ask
from hospitality.ai.mentor import codex_upload
from hospitality.ai.mentor import quiz as mentor_quiz
from hospitality.ai.mentor.common import MAX_MESSAGE_CHARS, MentorEvent, MentorProviders
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.errors import INTERNAL_ERROR_CODE, AppError
from hospitality.shared.logging import get_logger
from hospitality.staff_portal import mentor
from hospitality.staff_portal.browser import require_json_same_origin

logger = get_logger(module=__name__)

router = APIRouter(prefix="/{tenant_slug}/api/mentor", tags=["staff-portal"])

# Загрузка кодекса: у «Наставника» 13 файлов и ~600 КБ текста; пределы — с запасом
# (знаков — на все файлы вместе).
MAX_CODEX_FILES: Final = 60
MAX_CODEX_FILE_CHARS: Final = 3_000_000
# Главы нумеруются подряд с 1; у «Наставника» их меньше сотни.
MAX_CHAPTER_NUMBER: Final = 10_000


def get_mentor_providers() -> MentorProviders:
    """Боевые провайдеры шлюза; тесты подменяют их Fake-адаптерами (ADR-007)."""
    return MentorProviders()


Providers = Annotated[MentorProviders, Depends(get_mentor_providers)]
RoleDependency = Callable[[Request], Awaitable[StaffContext]]


def _without_nul(value: str) -> str:
    """NUL Postgres в тексте не хранит: запись после оплаченного ответа упала бы (#531)."""
    if "\x00" in value:
        raise ValueError("нулевой символ в тексте")
    return value


class MessageBody(BaseModel):
    """Вопрос или ответ «Тренировки»: пустой, длиннее предела или с NUL — 422 (Н-11, #531)."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(max_length=MAX_MESSAGE_CHARS)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = _without_nul(value).strip()
        if not text:
            raise ValueError("пустой текст")
        return text


class QuizNewBody(BaseModel):
    """Тема «Нового вопроса»: глава, свободный текст или ничего (§5.5).

    Номер главы ограничен сверху: больше `Integer` — ошибка запроса к БД (#531).
    """

    model_config = ConfigDict(extra="forbid")

    chapter_number: int | None = Field(default=None, ge=1, le=MAX_CHAPTER_NUMBER)
    text: str | None = Field(default=None, max_length=200)

    @field_validator("text")
    @classmethod
    def _no_nul(cls, value: str | None) -> str | None:
        return None if value is None else _without_nul(value)


class ResetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: mentor_api.ConversationMode


class CodexFileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    text: str = Field(max_length=MAX_CODEX_FILE_CHARS)


class CodexUploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[CodexFileBody] = Field(min_length=1, max_length=MAX_CODEX_FILES)

    @field_validator("files")
    @classmethod
    def _total_size(cls, files: list[CodexFileBody]) -> list[CodexFileBody]:
        if sum(len(f.text) for f in files) > MAX_CODEX_FILE_CHARS:
            raise ValueError(f"кодекс длиннее {MAX_CODEX_FILE_CHARS} знаков")
        return files


class CodexUploadedView(BaseModel):
    title: str
    article_count: int


async def _actor(request: Request, role: RoleDependency = mentor.any_role) -> StaffContext:
    """CSRF и личность — до всего; затем раздел у отеля: выключен — 404."""
    require_json_same_origin(request)
    staff = await role(request)
    await mentor.require_section(staff)
    return staff


class MentorStreamResponse(StreamingResponse):
    """Поток ответа, который останавливается между кусками, а не посреди фазы хода.

    Starlette при ASGI 2.3 (uvicorn 0.54) отменяет обработчик в момент ухода
    клиента — отмена била в запрос к БД, эмбеддинг или резюме: SQLAlchemy писал
    ERROR, Sentry получал событие с текстом вопроса, вызов шлюза оставался без
    строки журнала (#531). Здесь уход только отмечается; ход доводит текущую фазу
    и закрывается на ближайшем куске — генератор получает `GeneratorExit` на
    `yield`, и шлюз пишет оборванный поток `aborted` (ADR-024 §5). Закрытие — сразу,
    пока контекст запроса жив: сборщик мусора финализировал бы ход вне тенанта.
    """

    body_iterator: AsyncGenerator[str, None]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        gone = anyio.Event()
        completed = False
        try:
            async with anyio.create_task_group() as watcher:
                # Как у Starlette: сервер ASGI 2.4 сообщает об уходе ошибкой `send`,
                # до 2.4 (uvicorn) — только сообщением `http.disconnect`.
                if _spec_version(scope) < (2, 4):
                    watcher.start_soon(_wait_for_disconnect, receive, gone)
                completed = await self._send_until_gone(send, gone)
                watcher.cancel_scope.cancel()
        except* OSError:
            completed = False  # клиент ушёл, сервер ASGI 2.4 сказал это ошибкой `send`
        finally:
            await self.body_iterator.aclose()
        if not completed:
            logger.info("mentor_stream_client_gone")

    async def _send_until_gone(self, send: Send, gone: anyio.Event) -> bool:
        """`True` — поток отдан до конца; `False` — клиент ушёл раньше."""
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        async for chunk in self.body_iterator:
            if gone.is_set():
                return False
            body = chunk.encode(self.charset)
            await send({"type": "http.response.body", "body": body, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        return True


def _spec_version(scope: Scope) -> tuple[int, ...]:
    return tuple(map(int, scope.get("asgi", {}).get("spec_version", "2.0").split(".")))


async def _wait_for_disconnect(receive: Receive, gone: anyio.Event) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass
    gone.set()


def _frame(kind: str, payload: dict[str, object]) -> str:
    return f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _events(
    events: AsyncGenerator[MentorEvent, None], flow: str
) -> AsyncGenerator[str, None]:
    async with aclosing(events) as stream:
        try:
            async for event in stream:
                yield _frame(event.type, event.model_dump(mode="json", exclude={"type"}))
        except AppError as error:
            # WARNING, а не ERROR: это ожидаемый отказ хода, а ERROR унёс бы тело
            # запроса — текст сотрудника — в Sentry (#433).
            logger.warning("mentor_stream_failed", flow=flow, error_code=error.code)
            yield _frame("error", {"code": error.code, "message": mentor.error_text(error)})
        except Exception as error:
            # Ответ уже начат (200 ушёл) — обработчик 500 сюда не дотянется. Сотруднику —
            # событие `error`; в лог — только тип: текст исключения БД несёт параметры
            # запроса, то есть текст сотрудника (PII_REGISTRY), а ERROR — тело в Sentry (#433).
            logger.warning(
                "mentor_stream_failed",
                flow=flow,
                error_code=INTERNAL_ERROR_CODE,
                error_type=type(error).__name__,
            )
            yield _frame("error", {"code": INTERNAL_ERROR_CODE, "message": mentor.UNAVAILABLE_TEXT})


def _stream(events: AsyncGenerator[MentorEvent, None], flow: str) -> MentorStreamResponse:
    return MentorStreamResponse(
        _events(events, flow),
        media_type="text/event-stream",
        # no-store — ответ несёт текст диалога; X-Accel-Buffering — прокси не копит поток.
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/ask", response_class=StreamingResponse)
async def ask(request: Request, body: MessageBody, providers: Providers) -> MentorStreamResponse:
    """«Спросить»: ответ по статьям кодекса потоком (§5.1–5.4)."""
    staff = await _actor(request)
    return _stream(mentor_ask.ask(staff.user_id, body.message, providers=providers), "ask")


@router.post("/quiz/new", response_class=StreamingResponse)
async def quiz_new(
    request: Request, body: QuizNewBody, providers: Providers
) -> MentorStreamResponse:
    """«Новый вопрос» «Тренировки» потоком (§5.5)."""
    staff = await _actor(request)
    topic = mentor_quiz.QuizTopic(chapter_number=body.chapter_number, text=body.text)
    return _stream(
        mentor_quiz.new_quiz_question(staff.user_id, topic, providers=providers), "quiz_question"
    )


@router.post("/quiz/answer", response_class=StreamingResponse)
async def quiz_answer(
    request: Request, body: MessageBody, providers: Providers
) -> MentorStreamResponse:
    """«Проверить» ответ «Тренировки» потоком (§5.5); вопроса нет — событие `error`."""
    staff = await _actor(request)
    return _stream(
        mentor_quiz.check_quiz_answer(staff.user_id, body.message, providers=providers),
        "quiz_check",
    )


@router.post("/reset")
async def reset(request: Request, body: ResetBody) -> dict[str, bool]:
    """«Начать заново» (§5.6): диалог режима целиком — только свой."""
    staff = await _actor(request)
    deleted = await mentor_api.delete_conversation(staff.user_id, body.mode)
    logger.info("mentor_conversation_reset", mode=body.mode.value, deleted=deleted)
    return {"deleted": deleted}


@router.post("/codex")
async def upload_codex(
    request: Request, body: CodexUploadBody, providers: Providers
) -> CodexUploadedView:
    """Загрузить кодекс (менеджер, §6): ошибка разметки — 422 ERR-MENTOR-001, старый остаётся."""
    staff = await _actor(request, mentor.manager_role)
    info = await codex_upload.upload_codex(
        [mentor_api.CodexFile(name=f.name, text=f.text) for f in body.files],
        uploaded_by_user_id=staff.user_id,
        providers=providers,
    )
    return CodexUploadedView(title=info.title, article_count=info.article_count)


@router.post("/stats/reset")
async def reset_stats(request: Request) -> dict[str, int]:
    """«Обнулить статистику» (менеджер, §6): все оценки «Тренировки» отеля."""
    await _actor(request, mentor.manager_role)
    return {"deleted": await mentor_api.reset_quiz_stats()}
