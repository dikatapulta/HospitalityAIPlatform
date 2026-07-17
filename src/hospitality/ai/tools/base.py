"""Общие типы AI-инструментов (Task 0015, FOUNDATION §7.3, P-9)."""

from __future__ import annotations

import enum


class ConfirmationClass(enum.StrEnum):
    """Класс подтверждения инструмента (P-9) — свойство его контракта.

    - `AUTO` — информация и черновики: исполняется без подтверждения.
    - `CONFIRM_GUEST` — действие по запросу гостя (заявка, такси):
      подтверждает гость перед исполнением.
    - `CONFIRM_STAFF` — деньги, документы, изменение брони (NG-4):
      подтверждает сотрудник. В Phase 0 таких инструментов ещё нет.

    Гейт исполнения по классу — забота оркестратора, а не текста промпта.
    """

    AUTO = "auto"
    CONFIRM_GUEST = "confirm_guest"
    CONFIRM_STAFF = "confirm_staff"
