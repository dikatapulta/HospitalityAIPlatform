"""Предел тела HTTP-запроса — один на всё приложение (issue #968, находка Л-3 аудита 10.11).

CANONICAL: тело любого запроса — не больше `DEFAULT_MAX_BODY_BYTES`; больше — 413
`ERR-PLATFORM-011` до разбора. Маршрут, которому нужно больше (файлы, кодекс «Наставника»),
читает тело сам и поднимает предел перед чтением — `raise_body_limit(request, n)`; так делает
`platform/upload_form.read_upload_form`. Поднимают после входа и прав: большое тело
не должно разбираться раньше, чем стало ясно, кто его прислал.

Зачем: без предела аноним шлёт десятки мегабайт JSON на любую дверь, где тело разбирается
раньше остального, и единственный воркер uvicorn секундами занят разбором, а память растёт
сотнями мегабайт (замер аудита — issue #968).

Проверка — при каждом чтении тела, то есть уже после маршрутизации: маршрут успевает поднять
предел. Объявленная `Content-Length` больше предела — отказ без чтения; без длины или с ложной
длиной режет счёт прочитанных байтов. Тело, которое приложение не читает, предела не касается:
uvicorn копит из сокета не больше 64 КиБ и дальше ждёт, пока приложение не попросит.
"""

from __future__ import annotations

from typing import Final

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# JSON-действия кабинета, гостевой чат, формы входа, вебхуки — килобайты; 1 МиБ — с запасом.
DEFAULT_MAX_BODY_BYTES: Final = 1024 * 1024

BODY_TOO_LARGE_ERROR_CODE: Final = "ERR-PLATFORM-011"

_SCOPE_KEY: Final = "hospitality.body_limit"


class RequestBodyTooLargeError(HTTPException):
    """413: тело больше предела; конверт ответа ставит `shared/errors.py`.

    Подкласс `HTTPException`, потому что FastAPI пропускает из разбора тела как есть только
    его — любое другое исключение там становится 400 «There was an error parsing the body».
    """

    def __init__(self, *, limit_bytes: int, content_length: int | None) -> None:
        super().__init__(status_code=413, detail="Request body is too large")
        self.limit_bytes = limit_bytes
        self.content_length = content_length


class _Budget:
    """Предел текущего запроса; маршрут поднимает его через `raise_body_limit`."""

    __slots__ = ("limit_bytes",)

    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes


def raise_body_limit(request: Request, limit_bytes: int) -> None:
    """Разрешить этому запросу тело до `limit_bytes` — до первого чтения тела.

    Без прослойки (приложение собрано не `create_app`) — ничего не делает.
    """
    budget = request.scope.get(_SCOPE_KEY)
    if isinstance(budget, _Budget):
        budget.limit_bytes = limit_bytes


class BodyLimitMiddleware:
    """Чистый ASGI, как `CorrelationIdMiddleware`: оборачивает только `receive`."""

    def __init__(self, app: ASGIApp, *, default_limit_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self._app = app
        self._default_limit_bytes = default_limit_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        budget = _Budget(self._default_limit_bytes)
        scope[_SCOPE_KEY] = budget
        content_length = _content_length(scope)
        received = 0

        async def receive_within_limit() -> Message:
            nonlocal received
            if content_length is not None and content_length > budget.limit_bytes:
                raise RequestBodyTooLargeError(
                    limit_bytes=budget.limit_bytes, content_length=content_length
                )
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > budget.limit_bytes:
                    raise RequestBodyTooLargeError(
                        limit_bytes=budget.limit_bytes, content_length=content_length
                    )
            return message

        await self._app(scope, receive_within_limit, send)


def _content_length(scope: Scope) -> int | None:
    declared = Headers(scope=scope).get("content-length")
    if declared is None or not declared.isdigit():
        # Негодную длину не доверяем и не отвергаем здесь: её режет счёт байтов.
        return None
    return int(declared)
