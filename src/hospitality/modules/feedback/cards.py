"""Карточка пальца вниз в службе эскалаций (spec 0076 §4.3, тексты — §8).

Заявка заводится через `requests_api.create_request` с `origin=feedback`: так она
получает всё, что есть у заявки, — «Взять», лестницу службы, место в очереди и в
сводке дня. Привязки к диалогу (`request_origins`) у неё нет, как у карточки
эскалации (spec 0039 §2.7): гостю не придёт «ваша заявка выполнена» про карточку.

Здесь только сборка и запись карточки; best-effort, логи исхода и запись
`card_request_id` — у вызывающего (`service.py`). Ошибка наружу — исключением.
"""

from __future__ import annotations

import uuid
from typing import Final

from hospitality.modules.requests import api as requests_api
from hospitality.shared.errors import AppError

# Тексты персоналу — §8 спеки, решение основателя 10.11.2026 «Да, как у окна».
# Подстановка — `str.replace`, а не `format`: в названии службы и в тексте гостя
# могут быть фигурные скобки. `{text}` обрезается с «…», чтобы итог влез в поле.
_CARD_SUMMARY: Final = "Гость недоволен заявкой {number}«{text}» — {service}"
_SECOND_CARD_SUMMARY: Final = "Гость дописал к оценке заявки {number}«{text}»"
_CLOSED_BY_LINE: Final = "Закрыл: {name}"
_COMMENT_LINE: Final = "Гость написал: «{text}»"

# Предел `ServiceRequestCreate.summary`: схема отвергла бы длинное значение
# целиком, а суть оценённой заявки может занять весь предел. Подробности в предел
# не упираются: имя ≤ 255 и комментарий ≤ 500 знаков.
_SUMMARY_MAX_CHARS: Final = 500


async def open_card(
    rated: requests_api.ServiceRequestRead, *, escalation_key: str, comment: str | None
) -> uuid.UUID:
    """Завести карточку «Гость недоволен заявкой #N …»; вернуть её id.

    `comment` — уже замаскированный текст гостя: карточку заводит комментарий,
    когда первая попытка при оценке не удалась (запись «потом» ревью PR #914).
    """
    category_id = await _category_id(escalation_key)
    template = _CARD_SUMMARY.replace("{number}", _number(rated.daily_number, " ")).replace(
        "{service}", await _service_name(rated.category_id)
    )
    summary, _ = _fill(template, rated.summary)
    lines = []
    if rated.closed_by_display_name:
        lines.append(_CLOSED_BY_LINE.replace("{name}", rated.closed_by_display_name))
    if comment:
        lines.append(_COMMENT_LINE.replace("{text}", comment))
    created = await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=category_id,
            origin=requests_api.ServiceRequestOrigin.FEEDBACK,
            summary=summary,
            details="\n".join(lines) or None,
            room_number=rated.room_number,
        ),
        stay_id=rated.stay_id,
    )
    return created.id


async def append_comment(
    card_id: uuid.UUID,
    rated: requests_api.ServiceRequestRead,
    *,
    escalation_key: str,
    comment: str,
) -> tuple[uuid.UUID, bool]:
    """Дописать комментарий в невзятую карточку, иначе — вторая карточка (§4.3).

    Вернуть id карточки, где теперь комментарий, и True, если он дописан в первую.
    Карточку уже взяли, закрыли или она полна — канон spec 0039 §2.5: новая
    карточка, а не потерянный комментарий.
    """
    line = _COMMENT_LINE.replace("{text}", comment)
    if await requests_api.append_request_details(card_id, line) is not None:
        return card_id, True
    summary, truncated = _fill(
        _SECOND_CARD_SUMMARY.replace("{number}", _number(rated.daily_number, ": ")), comment
    )
    created = await requests_api.create_request(
        requests_api.ServiceRequestCreate(
            category_id=await _category_id(escalation_key),
            origin=requests_api.ServiceRequestOrigin.FEEDBACK,
            summary=summary,
            # Суть обрезана — полный текст гостя остаётся в подробностях.
            details=line if truncated else None,
            room_number=rated.room_number,
        ),
        stay_id=rated.stay_id,
    )
    return created.id, False


async def _category_id(category_key: str) -> uuid.UUID:
    """Id службы эскалаций; её нет у отеля (удалили руками) — ERR-REQUESTS-001."""
    for category in await requests_api.list_categories():
        if category.key == category_key:
            return category.id
    raise AppError(
        code=requests_api.ERR_REQUESTS_CATEGORY_NOT_FOUND,
        message="Escalation category not found",
        status_code=404,
    )


async def _service_name(category_id: uuid.UUID) -> str:
    """Название службы оценённой заявки; службы уже нет — прочерк: карточка важнее."""
    for category in await requests_api.list_categories():
        if category.id == category_id:
            return category.name
    return "—"


def _number(daily_number: int | None, tail: str) -> str:
    """«#12 » перед сутью; у заявки без дневного номера — ничего."""
    return f"#{daily_number}{tail}" if daily_number is not None else ""


def _fill(template: str, value: str) -> tuple[str, bool]:
    """Подставить `{text}`, обрезав его с «…» до предела сути; второе — обрезан ли."""
    free = _SUMMARY_MAX_CHARS - len(template.replace("{text}", ""))
    truncated = len(value) > free
    if truncated:
        value = value[: free - 1] + "…"
    return template.replace("{text}", value), truncated
