"""Общие типы AI-инструментов (Task 0015, FOUNDATION §7.3, P-9; spec 0025)."""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from hospitality.modules.requests import api as requests_api
from hospitality.platform.service_quotas import ServiceQuota

# Аргумент, который пишет только код инструмента (spec 0056 §4): платное
# предложение сверх квоты — штуки и цена из конфига. Присланное моделью под
# этим ключом выбрасывается у каждого вызова, до гейта P-9 и до `prepare`
# (`without_code_only_arguments`): иначе срочный вызов донёс бы выдуманную
# цену до заявки и до реплики гостю.
QUOTA_OFFER_ARGUMENT: Final = "quota_offer"
CODE_ONLY_ARGUMENTS: Final = frozenset({QUOTA_OFFER_ARGUMENT})


def without_code_only_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Аргументы модели без ключей, которые вправе писать только код."""
    return {key: value for key, value in arguments.items() if key not in CODE_ONLY_ARGUMENTS}


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
    """Контекст текущего хода для исполнения инструментов (spec 0025, spec 0027).

    `active_requests` — снапшот открытых заявок ЭТОГО диалога на ЭТОТ ход.
    По нему `cancel_service_request` повторно валидирует `request_id` на
    исполнении: id вне списка (чужая заявка, устаревший pending_action) —
    ERR-AI-004, а не тихая отмена. Тенантную изоляцию держит RLS (P-4),
    диалоговую — этот список.

    `verified_room_number` — комната из ПРИВЯЗКИ гостя (Stay веб-чата,
    spec 0027 §3.2), не со слов: при наличии `create_service_request`
    перезаписывает ею комнату из аргументов модели — «я вообще-то из 505-го»
    не переадресует заявку в чужой номер. None — канал без привязки (Telegram
    Phase 1) — прежнее поведение. `verified_stay_id` — тот же Stay привязки:
    `create_service_request` пишет его в заявку (spec 0054).

    `service_quotas` — квоты бесплатного из конфига ХОДА (spec 0056 §4): их
    читает `prepare` на ходе предложения. На ходе «да» конфиг не читается, и
    кортеж пуст — всё, что нужно исполнению, `prepare` уже положил в аргументы.
    """

    active_requests: tuple[ActiveRequest, ...] = ()
    verified_room_number: str | None = None
    verified_stay_id: uuid.UUID | None = None
    service_quotas: tuple[ServiceQuota, ...] = ()

    def find_active_request(self, request_id: uuid.UUID) -> ActiveRequest | None:
        """Заявка снапшота по id; None — id не из этого диалога/уже закрыта."""
        return next((r for r in self.active_requests if r.id == request_id), None)


@dataclass(frozen=True)
class PreparedCall:
    """Вызов инструмента после `prepare` — до гейта P-9 (spec 0056 §4).

    `arguments` — то, что уйдёт в `pending_action` и в `execute`: инструмент
    вправе их поправить (служба правила, штуки по остатку, строка для
    сотрудника). `question` — вопрос гостю вместо `confirmation_question`
    модели; None — вопрос модели, как раньше. `refusal` — ход становится
    ответом без гейта: исполнять нечего (норма исчерпана, платного нет).
    """

    arguments: dict[str, Any] = field(default_factory=dict)
    question: str | None = None
    refusal: str | None = None


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
