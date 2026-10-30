"""Маскирование платёжных паттернов в свободном тексте (spec 0031, NG-3).

CANONICAL: единственный канон маскирования PAN в тексте (P-12). Первый
потребитель — контракт нормализации каналов (`channels/base.py`): номер карты
гостя маскируется до записи в `messages` и до передачи в LLM (issue #128).
Маскирование PII в логах (issue #13) обязано переиспользовать этот модуль —
поэтому он в kernel, а не в `channels/`.

Чистые функции без I/O: ни логов, ни настроек — вызывается в валидаторе
Pydantic и не должен иметь побочных эффектов.
"""

from __future__ import annotations

import re

# Ряд — группы цифр, между соседними группами ровно один пробел, дефис или точка
# (так карту и набирают: «4400 4301 2345 6789»). Ряд берётся целиком, любой
# длины: к карте приклеиваются срок, номер комнаты, лишняя цифра (issue #239).
_DIGIT_RUN = re.compile(r"\d+(?:[ .-]\d+)*")
_DIGIT_GROUP = re.compile(r"\d+")
_PAN_MIN_DIGITS = 13
_PAN_MAX_DIGITS = 19


def _passes_luhn(digits: str) -> bool:
    """Контрольная сумма Луна (ISO/IEC 7812) — ей соответствует любой PAN."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_spans(groups: list[re.Match[str]]) -> list[tuple[int, int]]:
    """Отрезки ряда (индексы первой и последней группы), которые закрываются.

    Кандидат — подряд идущие **целые** группы, всего 13–19 цифр: края карты
    совпадают с краями групп, как бы её ни набрали. Закрывается каждый
    кандидат, проходящий Луна, а пересекающиеся сливаются — так карта уходит
    целиком, даже если её задел случайный кандидат подлиннее.
    """
    spans: list[tuple[int, int]] = []
    for first in range(len(groups)):
        digits = ""
        for last in range(first, len(groups)):
            digits += groups[last].group()
            if len(digits) > _PAN_MAX_DIGITS:
                break
            if len(digits) >= _PAN_MIN_DIGITS and _passes_luhn(digits):
                if spans and first <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], last))
                else:
                    spans.append((first, last))
    return spans


def _mask_run(match: re.Match[str]) -> str:
    run = match.group()
    groups = list(_DIGIT_GROUP.finditer(run))
    parts: list[str] = []
    cursor = 0
    for first, last in _card_spans(groups):
        start, end = groups[first].start(), groups[last].end()
        digits = re.sub(r"\D", "", run[start:end])
        parts.append(run[cursor:start])
        parts.append(f"[card ****{digits[-4:]}]")
        cursor = end
    parts.append(run[cursor:])
    return "".join(parts)


def mask_payment_card_numbers(text: str) -> str:
    """Заменить похожие на номер карты последовательности на `[card ****1234]`.

    Маскируется только кандидат, проходящий Луна: телефоны и номера броней
    короче 13 цифр, а трек-номера и прочие случайные ряды отсеивает контрольная
    сумма (~90 %). Последние 4 цифры сохраняются — отраслевой стандарт: видно,
    о какой карте речь, восстановить PAN нельзя. Ложные срабатывания приняты
    спекой 0031 (например, IMEI — он тоже по Луну).
    """
    return _DIGIT_RUN.sub(_mask_run, text)
