"""Форма с файлами из HTTP-запроса — как маршрут читает тело (spec 0047 §4, spec 0049 Р-8).

CANONICAL: маршрут, принимающий файлы от гостя или персонала, читает тело только
`read_upload_form` — не `request.form()` и не параметрами `UploadFile`; дальше
каждый файл идёт в `files.store_upload`.

Зачем своё (ревью PR #495, Н-2): `request.form()` Starlette кладёт файл больше
1 МиБ во временный файл `/tmp` — в контейнере это overlay-диск, и документ гостя
лёг бы на него открытым текстом ещё до шифрования; а тело он читает без потолка.
Здесь:

- потолок тела — до разбора: `Content-Length` больше потолка — отказ без чтения;
  тело без длины или с ложной длиной режет счёт байтов потока;
- файлы держатся в памяти — порог выгрузки на диск равен потолку тела, и на
  диск не ложится ничего. `/tmp` контейнера `app` ещё и tmpfs в обоих compose —
  вторая стена, если кто-то всё же позовёт `request.form()`;
- число файлов и полей — потолки вызывающего и этого модуля.

Содержимое файлов здесь не проверяется — это `store_upload` (песочница).
Отказы — исключения ниже; код ошибки и текст выбирает маршрут.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Final

from starlette.datastructures import Headers, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import Request

# Текстовые поля формы — короткие (имя, комментарий): 64 КиБ с запасом на
# 2000 знаков в любой кодировке, 16 полей — на любую форму платформы.
_MAX_FIELD_BYTES: Final = 64 * 1024
_MAX_FIELDS: Final = 16


class UploadFormError(Exception):
    """Тело не стало формой; маршрут превращает подкласс в свой код ошибки."""


class UploadBodyTooLargeError(UploadFormError):
    """Тело больше потолка — по `Content-Length` или по счёту байтов."""


class UploadTooManyFilesError(UploadFormError):
    """Файлов больше, чем разрешил маршрут."""


class UploadFormMalformedError(UploadFormError):
    """Не multipart, битая разметка, лишние или слишком длинные поля."""


@dataclass(frozen=True)
class UploadedPart:
    """Файл формы: имя у отправителя и байты (проверяет `store_upload`)."""

    filename: str
    data: bytes


@dataclass(frozen=True)
class UploadForm:
    fields: dict[str, str]
    files: list[UploadedPart]


async def read_upload_form(request: Request, *, max_body_bytes: int, max_files: int) -> UploadForm:
    """Прочитать multipart-тело запроса в память с потолками; негодное — `UploadFormError`."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_body_bytes:
                raise UploadBodyTooLargeError(f"content-length {declared}")
        except ValueError as exc:
            raise UploadFormMalformedError("content-length is not a number") from exc
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise UploadFormMalformedError("not multipart/form-data")
    parser = _InMemoryMultiPartParser(
        request.headers,
        _counted(request.stream(), max_body_bytes),
        max_files=max_files,
        max_fields=_MAX_FIELDS,
        max_part_size=_MAX_FIELD_BYTES,
        spool_max_size=max_body_bytes,
    )
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        if str(exc).startswith("Too many files"):
            raise UploadTooManyFilesError(str(exc)) from exc
        raise UploadFormMalformedError(str(exc)) from exc
    fields: dict[str, str] = {}
    files: list[UploadedPart] = []
    try:
        for name, value in form.multi_items():
            if isinstance(value, UploadFile):
                data = await value.read()
                # Пустой выбор файла браузер шлёт частью без имени и байтов.
                if data or value.filename:
                    files.append(UploadedPart(filename=value.filename or "", data=data))
            else:
                fields[name] = value
    finally:
        await form.close()
    return UploadForm(fields=fields, files=files)


class _InMemoryMultiPartParser(MultiPartParser):
    """Разбор Starlette, но без выгрузки файлов на диск: порог — потолок тела."""

    def __init__(
        self,
        headers: Headers,
        stream: AsyncGenerator[bytes, None],
        *,
        max_files: int,
        max_fields: int,
        max_part_size: int,
        spool_max_size: int,
    ) -> None:
        super().__init__(
            headers,
            stream,
            max_files=max_files,
            max_fields=max_fields,
            max_part_size=max_part_size,
        )
        self.spool_max_size = spool_max_size


async def _counted(stream: AsyncIterator[bytes], limit: int) -> AsyncGenerator[bytes, None]:
    received = 0
    async for chunk in stream:
        received += len(chunk)
        if received > limit:
            raise UploadBodyTooLargeError(f"body over {limit} bytes")
        yield chunk
