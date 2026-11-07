"""Pydantic-схемы границ модуля guests (spec 0027 §1.4, R-6, P-7).

Сервисные функции принимают `*Start`/`*CheckIn` и возвращают `*Read`/`*Grant` —
ORM-объекты наружу модуля не выходят. `tenant_id` в схемах отсутствует
намеренно: тенанта задаёт контекст (P-4), вызывающая сторона его не выбирает.

Plaintext-секреты (`access_code`, `session_token`) живут ТОЛЬКО в результатах
операций, которые их порождают (`StayCheckInResult`, `GuestSessionGrant`), —
показываются один раз и нигде больше не читаются (в БД — хэши, ADR-008).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from hospitality.modules.guests.models import GuestIdentityKind, StaySource, StayStatus

# Потолок числа гостей одного проживания (spec 0033 §6): опечатка «44» вместо
# «4» не должна раздуть квоты #124. Общий для кабинета и CLI.
MAX_GUESTS_PER_STAY: Final = 20

# Номер комнаты без «/»: маршрут чата комнаты `/g/{slug}/{room}` веб-канала не
# совпадает даже с `%2F` — гость получил бы 404 (#359). Тот же запрет — у строки
# снимка PMS (`shared/ports/pms.py`) и у «Переселить» кабинета.
ROOM_NUMBER_PATTERN: Final = r"^[^/]+$"


class StayCheckIn(BaseModel):
    """Данные заселения (CLI, кнопка «Заселить» кабинета, заезд из Opera, spec 0033 §6)."""

    room_number: str = Field(min_length=1, max_length=20, pattern=ROOM_NUMBER_PATTERN)
    # UTC (§9); перевод «12:00 по времени отеля» → UTC — забота вызывающего.
    check_out_at: datetime
    guest_display_name: str | None = Field(default=None, max_length=255)
    # Кнопки «1–4» и поле «другое» кабинета, `--guests` CLI; вход квот #124.
    guests_count: int = Field(default=1, ge=1, le=MAX_GUESTS_PER_STAY)


class StayRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    guest_id: uuid.UUID
    room_number: str
    status: StayStatus
    guests_count: int
    check_in_at: datetime
    check_out_at: datetime
    # Из Opera или вручную (spec 0058 §2.5: карточка PMS-Stay предупреждает о снимке).
    source: StaySource = StaySource.MANUAL
    pms_booking_ref: str | None = None
    # Последнее продление: когда и с какого срока (spec 0062 §3); None — не продлевали.
    extended_at: datetime | None = None
    extended_from: datetime | None = None


class StayCheckInResult(BaseModel):
    """Итог заселения: Stay + код заселения ОДИН раз в открытом виде (§1.2)."""

    stay: StayRead
    access_code: str


class StayArrival(BaseModel):
    """Заселение для списка кабинета «Заселены за день» (#832): Stay любого
    статуса и подключался ли к нему гость."""

    stay: StayRead
    # Хоть одна сессия, включая погашенные выездом (`close_stay`): у выехавшего
    # живых привязок нет, а «подключился ли» ресепшену важно и после выезда. У
    # действующего Stay это то же число, что «Подключений: N» карточки, — сессию
    # гасит только выезд.
    guest_connected: bool


class GuestSessionStart(BaseModel):
    """Запрос привязки канала к Stay: тройка тенант+комната+код (ADR-008 §3).

    Тенант — из контекста (P-4). `identity_external_id` — идентификатор
    клиента в канале (web: UUID, порождённый каналом; telegram: chat_id —
    когда Telegram перейдёт на auth-only). `consent_version` — версия текста
    согласия, показанного при привязке (юраудит 22.07).
    """

    room_number: str = Field(min_length=1, max_length=20)
    code: str = Field(min_length=1, max_length=32)
    identity_kind: GuestIdentityKind = GuestIdentityKind.WEB
    identity_external_id: str = Field(min_length=1, max_length=128)
    consent_version: str = Field(min_length=1, max_length=16)


class GuestSessionBind(BaseModel):
    """Запрос привязки по ссылке с листка (spec 0033 §6).

    Право на Stay даёт токен ссылки, выпущенной персоналом, — комнаты и кода
    здесь нет. Остальные поля — те же, что у `GuestSessionStart`: привязка
    создаёт идентичность и сессию тем же путём (P-12).
    """

    # token_urlsafe(32) — 43 символа; потолок с запасом, длиннее — не наш токен.
    bind_token: str = Field(min_length=1, max_length=128)
    identity_kind: GuestIdentityKind = GuestIdentityKind.WEB
    identity_external_id: str = Field(min_length=1, max_length=128)
    consent_version: str = Field(min_length=1, max_length=16)
    # Сессия, которую браузер уже держит (cookie). Она того же Stay — привязка
    # берёт её идентичность, и гость возвращается в свой диалог (#223). Потолка
    # нет намеренно: значение только хэшируется для поиска, а чужая или битая
    # cookie не должна ронять вход ошибкой схемы.
    current_session_token: str | None = None


class GuestSessionGrant(BaseModel):
    """Успешная привязка: opaque-токен сессии ОДИН раз в открытом виде."""

    session_token: str
    stay_id: uuid.UUID
    guest_identity_id: uuid.UUID
    room_number: str


class ActiveGuestSession(BaseModel):
    """Валидная сессия на текущем действии (`resolve_session`, ADR-008 §3)."""

    session_id: uuid.UUID
    stay_id: uuid.UUID
    guest_identity_id: uuid.UUID
    room_number: str
    check_out_at: datetime


class PmsDiscrepancyKind(enum.StrEnum):
    """Почему строку снимка нельзя применить (spec 0058 §2.2, §2.5)."""

    # Комната занята PMS-Stay другой брони, которая в снимке есть.
    OCCUPIED_BY_PMS = "occupied_by_pms"
    # Комната занята ручным Stay с другой датой заезда — другой гость.
    OCCUPIED_BY_MANUAL = "occupied_by_manual"
    # Бронь переехала в Opera, а комната назначения здесь занята.
    MOVE_BLOCKED = "move_blocked"


class PmsDiscrepancy(BaseModel):
    """Расхождение Opera и кабинета: система ничего не меняла, решает ресепшен.

    Список последнего применённого снимка лежит в `pms_sync_state.discrepancies`
    (ПД: номер брони — docs/PII_REGISTRY.md) и живёт до следующего снимка.
    `room_number` — комната, чью карточку открывает кнопка «Открыть».
    """

    kind: PmsDiscrepancyKind
    room_number: str
    booking_ref: str
    # Бронь Stay, занявшего комнату (OCCUPIED_BY_PMS).
    other_booking_ref: str | None = None
    # Заезд по Opera и дата ручного заселения по поясу отеля (OCCUPIED_BY_MANUAL).
    arrival: date | None = None
    manual_check_in: date | None = None
    # Куда бронь переехала в Opera (MOVE_BLOCKED); `room_number` — где Stay сейчас.
    target_room: str | None = None


class PmsStatus(BaseModel):
    """Что ресепшен видит о выгрузке Opera на странице заселения (spec 0058 §2.5).

    Только у тенанта с `pms_source: opera_report`. `taken_at` — последний
    принятый снимок (None — снимков ещё не было), `stale` — он старше трёх
    интервалов выгрузки (тот же порог, что у алерта ERR-GUESTS-010, §2.6);
    `held_closures` и `discrepancies` — из последнего применённого снимка.
    `last_applied_at` — когда сверка последний раз применила снимок (None — ни
    разу), `applied_stale` — это было раньше трёх интервалов назад: заезд из Opera
    становится проживанием только при применении, и доска «Номера» (spec 0061 §3)
    верит «свободен» по нему, а не по `taken_at`.
    """

    interval_minutes: int
    taken_at: datetime | None
    stale: bool
    held_closures: int
    discrepancies: list[PmsDiscrepancy]
    last_applied_at: datetime | None = None
    applied_stale: bool = False


class StayExtensionOutlook(BaseModel):
    """Что бот знает о продлении этого Stay из свободных номеров Opera (spec 0059 §2.3).

    `today`, `departure` и `checkout_hour` — по поясу отеля (дата и час выезда
    самого Stay); `updated_at` — когда принят файл (UTC); `known_through` — его
    последняя ночь; `full_nights` — ночи с `departure` по `known_through`, где
    свободных номеров нет. Номера гостя, броней и цены здесь нет.
    """

    model_config = ConfigDict(frozen=True)

    today: date
    departure: date
    checkout_hour: int = Field(ge=0, le=23)
    updated_at: datetime
    known_through: date
    full_nights: tuple[date, ...]
