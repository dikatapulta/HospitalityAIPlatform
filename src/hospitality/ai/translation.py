"""Перевод сути заявки на язык персонала (баг #71, замечание основателя).

Гость пишет на любом языке, а суть заявки в staff-чате читают сотрудники отеля
(в Казахстане — по-русски). Просить модель дать summary сразу на русском в том же
tool-call, где вопрос гостю на его языке, — НЕнадёжно (замер: два языка в одном
структурном вызове модель путает, китайский не переводит). Отдельный вызов с
единственной задачей «переведи на русский» надёжен (замер: Haiku и Sonnet
переводят zh/hi/tr/ar корректно). Оригинал показывается рядом с переводом как
эталон на случай осечки (channels/telegram/notifications.py).

Весь LLM-трафик — через `ai/gateway` (§7.2). Phase 1: язык персонала — настройка
тенанта (P-11); сейчас платформенный дефолт — русский (промпт `translate_to_staff_v1`).
"""

from __future__ import annotations

from hospitality.ai.gateway import api as gateway
from hospitality.ai.gateway.api import LlmMessage, LlmProvider, LlmRequest
from hospitality.ai.prompts import load_prompt

# Версия промпта — в имени файла (§7.5). Утилитарный промпт перевода, не диалоговый.
STAFF_TRANSLATION_PROMPT_NAME = "translate_to_staff_v1"


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
