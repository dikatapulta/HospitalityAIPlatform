"""Контракт нормализованного сообщения — общий для всех каналов (Task 0016, P-7).

CANONICAL: каждый канал (`channels/telegram`, будущие `whatsapp`, `email`, `web`)
приводит своё входящее сообщение к `NormalizedMessage` — единственному формату,
который видят слои выше (оркестратор, Task 0015/0017). Транспорт и разбор payload
провайдера — приватная деталь адаптера канала; наружу выходит только этот контракт.

Почему контракт, а не «словарь произвольной формы» (P-7): оркестратор и AI Gateway
не должны знать, из какого канала пришло сообщение. Новый канал = новый адаптер,
который заполняет эти же поля, — ноль изменений выше по стеку.

Каналы — НЕ порты ядра (§8): обязательного Fake-адаптера у них нет, в тестах канал
воспроизводится payload'ами провайдера. Поэтому контракт живёт в `channels/`
(композиционный слой), а не в `integrations/`.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field


class MessageKind(enum.StrEnum):
    """Тип входящего сообщения после нормализации.

    Phase 0 обрабатывает только текст; всё остальное (фото, стикер, голос,
    документ, локация) — `UNSUPPORTED`: канал вежливо отказывает и не тащит
    неразобранный контент выше. Разбор вложений — Phase 1+ (отдельные типы).
    """

    TEXT = "text"
    # Нажатие inline-кнопки (Telegram callback_query и аналоги других каналов):
    # `text` несёт callback-данные (`req:<uuid>:<действие>`), `reply_to` —
    # сообщение, под которым была кнопка (spec 0021 П-2). Появился для
    # staff-чата; каналы без кнопок его просто не порождают.
    CALLBACK = "callback"
    UNSUPPORTED = "unsupported"


class ReplyTo(BaseModel):
    """Ответ гостя на конкретное прошлое сообщение (reply), контракт зарезервирован.

    Зарезервировано в контракте с самого начала (DISCUSSION_LOG «Контракт
    нормализованного сообщения: reply-to»), чтобы паттерн был канонический (P-12)
    и WhatsApp-адаптеру в Phase 1 не пришлось переделывать контракт задним числом:

    - Telegram (Bot API) присылает во `Update.reply_to_message` ПОЛНЫЙ объект
      исходного сообщения — `text` заполняется сразу.
    - WhatsApp (Cloud API) присылает только `context.id` — адаптеру Phase 1
      придётся восстанавливать `text` по `external_message_id` из сохранённой
      истории; поэтому `text` необязателен.
    """

    model_config = ConfigDict(frozen=True)

    external_message_id: str
    text: str | None = None


class NormalizedMessage(BaseModel):
    """Входящее сообщение гостя в канале, приведённое к единому виду (P-7).

    `frozen=True`: нормализованное сообщение — значение; адаптер собирает его один
    раз из payload провайдера и передаёт дальше неизменным.
    """

    model_config = ConfigDict(frozen=True)

    # Имя канала ("telegram") — попадает в Conversation.channel и логи.
    channel: str = Field(min_length=1, max_length=32)
    # Идентификатор чата гостя внутри канала (Telegram chat.id как строка) —
    # ключ Conversation. В Phase 0 гость = строка Conversation (модуля guests нет).
    chat_id: str = Field(min_length=1, max_length=128)
    # Идентификатор самого сообщения у провайдера (Telegram message_id) —
    # хранится на Message; по нему Phase 1 восстановит reply_to для WhatsApp.
    external_message_id: str = Field(min_length=1, max_length=128)
    # Ключ идемпотентности доставки (P-8): Telegram update_id, namespace'нутый
    # ("telegram:update:<id>"), чтобы не коллизировать с ключами других каналов.
    # Повторная доставка того же вебхука несёт тот же ключ — дубликат отсеивается.
    idempotency_key: str = Field(min_length=1, max_length=128)
    kind: MessageKind
    # Текст сообщения при kind == TEXT; callback-данные кнопки при kind == CALLBACK.
    text: str | None = None
    # Reply-контекст: ответ на конкретное сообщение (см. ReplyTo); у CALLBACK —
    # сообщение, под которым нажата кнопка (кнопка ≈ ответ на своё сообщение).
    reply_to: ReplyTo | None = None
    # Только для CALLBACK: id callback-запроса провайдера — им канал отвечает
    # «тостом» (Telegram answerCallbackQuery). None у обычных сообщений.
    callback_id: str | None = None
    # Автор действия во внешней системе (Telegram from.id) — для структурных
    # логов «кто нажал/скомандовал»; привязка к User/RBAC — Phase 1 (§17.7).
    actor_external_id: str | None = None
