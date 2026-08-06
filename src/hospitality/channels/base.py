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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hospitality.shared.pii import mask_payment_card_numbers


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

    # Цитата reply — тот же гостевой текст (Telegram присылает оригинал целиком):
    # платёжные паттерны маскируются, как в NormalizedMessage.text (spec 0031).
    @field_validator("text")
    @classmethod
    def _mask_payment_data(cls, value: str | None) -> str | None:
        return None if value is None else mask_payment_card_numbers(value)


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
    # Текст ДО маскирования — как его прислал провайдер. Заполняет валидатор
    # ниже, каналы это поле не передают; `exclude`/`repr=False` держат сырой PAN
    # вне дампов и логов. ЕДИНСТВЕННЫЙ законный потребитель — разбор команды
    # персонала (`channels/telegram/staff.py`, spec 0031 §2): маскирование карт
    # иногда калечит uuid заявки в `/done <id>` (issue #172). Всё, что хранится
    # или уходит в модель, берёт `text` — маскированный.
    raw_text: str | None = Field(default=None, exclude=True, repr=False)
    # Только для CALLBACK: id callback-запроса провайдера — им канал отвечает
    # «тостом» (Telegram answerCallbackQuery). None у обычных сообщений.
    callback_id: str | None = None
    # Автор действия во внешней системе (Telegram from.id) — для структурных
    # логов «кто нажал/скомандовал»; привязка к User/RBAC — Phase 1 (§17.7).
    actor_external_id: str | None = None
    # Язык клиента автора, как его сообщает провайдер (Telegram from.language_code,
    # BCP-47: "ru", "en-GB"). Единственный доступный признак языка ДО вызова LLM —
    # им выбирается язык экрана согласия и приветствия (spec 0029 §3). None —
    # провайдер языка не сообщает (web) или поля нет: тогда показываются все
    # поддерживаемые версии текста, а не угаданная одна.
    actor_language: str | None = Field(default=None, max_length=35)

    # NG-3 / issue #128 (spec 0031): номер карты, присланный гостем, маскируется
    # В МОМЕНТ нормализации — раньше любой записи в БД, LLM-хода и события
    # эскалации. Валидатор на контракте, а не в store/guest_turn: любой канал
    # обязан произвести NormalizedMessage, забыть маскирование невозможно.
    # CALLBACK исключён: там `text` — машинные callback-данные (`req:<uuid>:…`),
    # а не гостевой текст; ~0.2 % uuid содержат Луна-валидный ряд цифр, и
    # маскирование ломало бы разбор кнопки (класс отказа инцидента #143).
    # Оригинал остаётся в `raw_text` — тем же ~0.2 % калечились uuid в командах
    # персонала, а они приходят обычным TEXT и исключением не закрывались (#172).
    @model_validator(mode="after")
    def _mask_payment_data(self) -> NormalizedMessage:
        if self.text is None:
            return self
        # frozen-модель: валидатор — единственное место, где поля дозаполняются.
        object.__setattr__(self, "raw_text", self.text)
        if self.kind is not MessageKind.CALLBACK:
            object.__setattr__(self, "text", mask_payment_card_numbers(self.text))
        return self
