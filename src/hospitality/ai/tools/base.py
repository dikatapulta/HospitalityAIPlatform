"""Общие типы AI-инструментов (Task 0015, FOUNDATION §7.3, P-9; spec 0025)."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from hospitality.modules.requests import api as requests_api


@dataclass(frozen=True)
class ActiveRequest:
    """Открытая заявка диалога в контексте хода (spec 0025).

    Заполняет КАНАЛ (владелец привязки `request_origins`, ADR-011) из данных
    `modules/requests`; читают оркестратор (снапшот в системном промпте) и
    инструменты (allowed-список отмены). AI-слой сам таблиц канала не читает —
    направление импортов `channels → ai` сохраняется.
    """

    id: uuid.UUID
    status: requests_api.RequestStatus
    summary: str
    daily_number: int | None = None
    room_number: str | None = None


@dataclass(frozen=True)
class ToolTurnContext:
    """Контекст текущего хода для исполнения инструментов (spec 0025).

    `active_requests` — снапшот открытых заявок ЭТОГО диалога на ЭТОТ ход.
    По нему `cancel_service_request` повторно валидирует `request_id` на
    исполнении: id вне списка (чужая заявка, устаревший pending_action) —
    ERR-AI-004, а не тихая отмена. Тенантную изоляцию держит RLS (P-4),
    диалоговую — этот список.
    """

    active_requests: tuple[ActiveRequest, ...] = ()

    def find_active_request(self, request_id: uuid.UUID) -> ActiveRequest | None:
        """Заявка снапшота по id; None — id не из этого диалога/уже закрыта."""
        return next((r for r in self.active_requests if r.id == request_id), None)


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
