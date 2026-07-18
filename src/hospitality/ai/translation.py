"""Переводы между языком гостя и языком персонала (баг #71, spec 0021).

Гость пишет на любом языке; персонал (пилот, Казахстан) читает по-русски.
Урок бага #71: просить модель работать на двух языках в ОДНОМ вызове —
ненадёжно (замер: языки путаются, китайский не переводится); отдельный вызов
с единственной задачей и единственным целевым языком надёжен (замер: Haiku и
Sonnet переводят zh/hi/tr/ar корректно). Поэтому здесь два зеркальных
однозадачных перевода:

- `translate_for_staff` — суть заявки → русский (staff-чат; оригинал
  показывается рядом как эталон, channels/telegram/notifications.py);
- `translate_for_guest` — статусное уведомление → язык гостя по ISO 639-1
  коду с заявки (spec 0021 П-1, issue #77).

Весь LLM-трафик — через `ai/gateway` (§7.2). Phase 1: язык персонала — настройка
тенанта (P-11); сейчас платформенный дефолт — русский (промпт `translate_to_staff_v1`).
"""

from __future__ import annotations

from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import LlmMessage, LlmProvider, LlmRequest
from hospitality.ai.prompts import load_prompt

# Версии промптов — в именах файлов (§7.5). Утилитарные промпты перевода, не диалоговые.
STAFF_TRANSLATION_PROMPT_NAME = "translate_to_staff_v1"
GUEST_TRANSLATION_PROMPT_NAME = "translate_to_guest_v1"

# Плейсхолдер целевого языка в промпте `translate_to_guest_v1`. Подстановка —
# `str.replace`, не `str.format`: случайные фигурные скобки в тексте промпта
# не должны становиться синтаксисом.
_LANGUAGE_PLACEHOLDER = "{language_code}"


async def translate_for_staff(text: str, *, provider: LlmProvider | None = None) -> str:
    """Перевести суть заявки на русский (язык персонала) для staff-чата.

    `provider` переопределяют тесты (Fake) и композиция; бизнес-код зовёт без него
    — боевая модель из настроек. Ошибку провайдера (`AppError`) НЕ глотает:
    решение о деградации (показать оригинал без перевода) принимает вызывающая
    сторона — уведомление службе не должно исчезнуть из-за сбоя перевода.
    """
    request = LlmRequest(
        messages=[LlmMessage(role="user", content=text)],
        system=load_prompt(STAFF_TRANSLATION_PROMPT_NAME),
    )
    response = await gateway.complete(request, provider=provider)
    return response.text.strip() or text.strip()


async def translate_for_guest(
    text: str, *, language_code: str, provider: LlmProvider | None = None
) -> str:
    """Перевести статусное сообщение на язык гостя (ISO 639-1 код с заявки).

    Один вызов — один целевой язык (урок #71). Ошибку провайдера (`AppError`)
    НЕ глотает: деградацию (отправить канонический русский текст) решает
    вызывающая сторона — уведомление гостю не должно исчезнуть из-за сбоя
    перевода (spec 0021 П-1).
    """
    system = load_prompt(GUEST_TRANSLATION_PROMPT_NAME).replace(
        _LANGUAGE_PLACEHOLDER, language_code
    )
    request = LlmRequest(messages=[LlmMessage(role="user", content=text)], system=system)
    response = await gateway.complete(request, provider=provider)
    return response.text.strip() or text.strip()
