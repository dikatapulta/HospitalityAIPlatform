"""Страница «Вид гостевого чата» (spec 0066 §2, §6; issue #761): данные и правки.

Выбор живёт в `TenantConfig.guest_chat_look`; правка — только `mutate_tenant_config`
(SELECT … FOR UPDATE), поэтому функции «конфиг → конфиг» исполняются под блокировкой
строки тенанта. Цвета и узоры — у веб-канала (`channels/web/look.py`), здесь только
надписи и порядок показа.

Знак отеля — файл узла `platform/files.py` (назначение `hotel_logo`). Замена — канон
`room_service/photos.py`: новый файл в узел → ссылка под блокировкой → удаление
прежнего после коммита. Упала запись ссылки — новый файл удаляется тут же; не удалился
прежний — WARNING: потеряно место на томе, а не данные. Две замены разом обе доходят:
вторая под блокировкой видит знак первой и удаляет его.

Предпросмотр — настоящая страница гостя в iframe (`channels/web/page.render_preview`):
её стили и подложка — те же, что увидит гость, без копии цветов в кабинете.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from hospitality.channels.web import look as web_look
from hospitality.channels.web import page as web_page
from hospitality.platform.config import TenantConfig, mutate_tenant_config
from hospitality.platform.files import FilePurpose, delete_file, store_upload
from hospitality.platform.guest_chat_look import (
    GuestChatLook,
    GuestChatPalette,
    GuestChatPattern,
)
from hospitality.platform.staff_auth import StaffContext
from hospitality.shared.db import platform_session_scope
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Образец переписки в телефоне предпросмотра (§7): строка комнаты, плашка дня, гость, отель.
PREVIEW_SAMPLE: Final = (
    "Номер 1507",
    "Сегодня",
    "Можно ещё два полотенца?",
    "Да, передали в хаускипинг, принесут в номер.",
)


def look_of(config: TenantConfig | None) -> GuestChatLook:
    """Выбор отеля; конфига нет (онбординг не завершён) — умолчание, как у гостя."""
    return config.guest_chat_look if config is not None else GuestChatLook()


def build_context(staff: StaffContext, config: TenantConfig | None) -> dict[str, Any]:
    """Контекст шаблона: варианты, узоры, знак и адреса действий."""
    look = look_of(config)
    base = f"/staff/{staff.tenant_slug}"
    return {
        "tenant_name": staff.tenant_name,
        "palettes": [
            {"key": key, "title": palette.title, "checked": key == look.palette}
            for key, palette in web_look.PALETTES.items()
        ],
        "patterns": [
            {"key": key, "title": title, "checked": key == look.pattern}
            for key, (title, _factor) in web_look.PATTERNS.items()
        ],
        "palette": look.palette,
        "pattern": look.pattern,
        "logo_url": web_look.logo_url(staff.tenant_slug, look),
        "initial": (staff.tenant_name.strip() or "Hotel")[0].upper(),
        "endpoint": f"{base}/api/chat-look",
    }


def preview_html(
    staff: StaffContext,
    config: TenantConfig | None,
    *,
    palette: GuestChatPalette,
    pattern: GuestChatPattern,
    night: bool,
) -> str:
    """Телефон предпросмотра: выбор со страницы (ещё не сохранён) и сохранённый знак."""
    look = look_of(config).model_copy(update={"palette": palette, "pattern": pattern})
    return web_page.render_preview(
        hotel_name=staff.tenant_name,
        look=look,
        logo_url=web_look.logo_url(staff.tenant_slug, look),
        night=night,
        sample=PREVIEW_SAMPLE,
    )


async def save_choice(
    staff: StaffContext, palette: GuestChatPalette, pattern: GuestChatPattern
) -> GuestChatLook:
    """«Сохранить»: вариант и узор; знак не трогается."""

    def mutate(config: TenantConfig) -> TenantConfig:
        look = config.guest_chat_look.model_copy(update={"palette": palette, "pattern": pattern})
        return config.model_copy(update={"guest_chat_look": look})

    async with platform_session_scope() as session:
        updated = await mutate_tenant_config(session, staff.tenant_id, mutate)
    logger.info(
        "staff.guest_chat_look_changed",
        palette=palette,
        pattern=pattern,
        user_id=str(staff.user_id),
    )
    return updated.guest_chat_look


async def _swap_logo(staff: StaffContext, new_id: uuid.UUID | None) -> uuid.UUID | None:
    """Поставить ссылку на знак под блокировкой; вернуть прежний id."""
    previous: list[uuid.UUID | None] = []

    def mutate(config: TenantConfig) -> TenantConfig:
        previous.append(config.guest_chat_look.logo_file_id)
        look = config.guest_chat_look.model_copy(update={"logo_file_id": new_id})
        return config.model_copy(update={"guest_chat_look": look})

    async with platform_session_scope() as session:
        await mutate_tenant_config(session, staff.tenant_id, mutate)
    return previous[-1]


async def set_logo(staff: StaffContext, data: bytes, original_name: str) -> None:
    """«Загрузить» / «Заменить»: негодный файл — ERR-FILES-001…003 текстом узла."""
    meta = await store_upload(
        FilePurpose.HOTEL_LOGO, data, original_name, uploaded_by_user_id=staff.user_id
    )
    try:
        previous = await _swap_logo(staff, meta.id)
    except BaseException:
        # Конфига нет или запись упала — файл без ссылки не нужен никому.
        await _delete_quietly(meta.id)
        raise
    if previous is not None:
        await _delete_quietly(previous)
    logger.info(
        "staff.guest_chat_logo_set",
        file_id=str(meta.id),
        replaced=previous is not None,
        user_id=str(staff.user_id),
    )


async def remove_logo(staff: StaffContext) -> None:
    """«Убрать»: ссылка снимается, файл удаляется. Знака нет — без изменений."""
    previous = await _swap_logo(staff, None)
    if previous is not None:
        await _delete_quietly(previous)
        logger.info("staff.guest_chat_logo_removed", user_id=str(staff.user_id))


async def _delete_quietly(file_id: uuid.UUID) -> None:
    """Удалить файл узла; не удалился — WARNING, а не исключение поверх исходного."""
    try:
        await delete_file(file_id)
    except Exception:
        logger.warning("guest_chat_logo_cleanup_failed", file_id=str(file_id), exc_info=True)
