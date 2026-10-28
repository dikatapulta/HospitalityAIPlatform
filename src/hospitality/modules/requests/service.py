"""Бизнес-логика модуля requests (Task 0012, FOUNDATION §5.2).

Единственный путь создать заявку или сменить её статус. Каждая функция —
одна транзакция по канону P-4/P-12: вызывается внутри `tenant_context`,
открывает `session_scope()`; события публикуются в той же транзакции,
что бизнес-запись (P-6). Ожидаемые ошибки — `AppError` с кодами каталога
(`docs/runbooks/errors.md`, R-8).
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import UTC, date, datetime, tzinfo
from typing import Any, Final, cast

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hospitality.modules.requests.events import (
    RequestCategoryChanged,
    RequestCreated,
    RequestInitiator,
    RequestStatusChanged,
)
from hospitality.modules.requests.models import RequestCategory, RequestStatus, ServiceRequest
from hospitality.modules.requests.schemas import (
    ActingUser,
    RequestCategoryCreate,
    RequestCategoryRead,
    ServiceRequestCreate,
    ServiceRequestPage,
    ServiceRequestRead,
)
from hospitality.platform.config import TENANT_NOT_CONFIGURED_ERROR_CODE, load_tenant_config
from hospitality.platform.staff_actions import StaffAction, StaffActionRecord, record_staff_action
from hospitality.shared.db import session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.events import publish
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Коды каталога ошибок (docs/runbooks/errors.md, R-8).
ERR_REQUESTS_CATEGORY_NOT_FOUND = "ERR-REQUESTS-001"
ERR_REQUESTS_REQUEST_NOT_FOUND = "ERR-REQUESTS-002"
ERR_REQUESTS_INVALID_STATUS_TRANSITION = "ERR-REQUESTS-003"
ERR_REQUESTS_CATEGORY_KEY_TAKEN = "ERR-REQUESTS-004"
ERR_REQUESTS_TRANSFER_REJECTED = "ERR-REQUESTS-005"
ERR_REQUESTS_DONE_WITHOUT_PAYMENT = "ERR-REQUESTS-006"
ERR_REQUESTS_FOREIGN_SERVICE = "ERR-REQUESTS-007"
ERR_REQUESTS_PAYMENT_NOT_APPLICABLE = "ERR-REQUESTS-008"

# Имя уникального индекса дневного номера (модель) — оно же опознаётся в тексте
# IntegrityError при гонке присвоения. Число попыток пересчёта номера с запасом:
# коллизия возможна лишь между одновременными создателями одного тенанта в один
# день (десятки в сутки) — практически 1 повтор, 5 хватает на любой всплеск.
_DAILY_NUMBER_CONSTRAINT: Final = "uq_service_requests_daily_number"
# Первичный ключ: его нарушает параллельный повтор с тем же `request_id` (P-8).
_PRIMARY_KEY_CONSTRAINT: Final = "pk_service_requests"
_MAX_DAILY_NUMBER_ATTEMPTS: Final = 5

# Чем становится summary обезличенной заявки (issue #42, spec 0032 §2): текст
# staff-facing (списки заявок читает персонал), без числа дней — срок живёт в
# настройке, а не в данных.
REQUEST_TEXT_ANONYMIZED_PLACEHOLDER: Final = "[обезличено: срок хранения истёк]"

# Жизненный цикл заявки (§5.2, ADR-013): new → in_progress → done/cancelled.
# Отменить можно любую незавершённую заявку; done и cancelled — терминальные.
# Взятую можно вернуть в очередь (spec 0042 §2.1): сотрудник ушёл со смены.
STATUS_TRANSITIONS: Final[dict[RequestStatus, frozenset[RequestStatus]]] = {
    RequestStatus.NEW: frozenset({RequestStatus.IN_PROGRESS, RequestStatus.CANCELLED}),
    RequestStatus.IN_PROGRESS: frozenset(
        {RequestStatus.DONE, RequestStatus.CANCELLED, RequestStatus.NEW}
    ),
    RequestStatus.DONE: frozenset(),
    RequestStatus.CANCELLED: frozenset(),
}

# Незакрытые статусы — те, из которых ещё есть переход (не done/cancelled).
# Выводится из карты переходов, чтобы не разъехаться с ней при правках.
_OPEN_STATUSES: Final = frozenset(
    status for status, transitions in STATUS_TRANSITIONS.items() if transitions
)

# Строка журнала изменений на каждый переход с личностью (spec 0041 §2.2).
# Карта полная: в любой статус, куда ведёт `STATUS_TRANSITIONS`, есть запись.
_STATUS_ACTIONS: Final[dict[RequestStatus, StaffAction]] = {
    RequestStatus.NEW: StaffAction.REQUEST_RETURNED,
    RequestStatus.IN_PROGRESS: StaffAction.REQUEST_CLAIMED,
    RequestStatus.DONE: StaffAction.REQUEST_DONE,
    RequestStatus.CANCELLED: StaffAction.REQUEST_CANCELLED,
}


async def create_category(data: RequestCategoryCreate) -> RequestCategoryRead:
    """Создать категорию заявок у текущего тенанта.

    В Phase 0 категории заводят сиды и тесты; с кабинетом персонала (Phase 1)
    сюда придёт HTTP API. Дубликат `key` у тенанта — ERR-REQUESTS-004.
    """
    category = RequestCategory(key=data.key, name=data.name)
    try:
        async with session_scope() as session:
            session.add(category)
            await session.flush()
    except IntegrityError as error:
        # Только нарушение уникальности (tenant_id, key) — «ключ занят»;
        # прочие IntegrityError (например, FK) — не ожидаемая бизнес-ошибка.
        if "uq_request_categories_tenant_id" not in str(error):
            raise
        raise AppError(
            code=ERR_REQUESTS_CATEGORY_KEY_TAKEN,
            message=f"Request category with key {data.key!r} already exists",
            status_code=409,
        ) from None
    logger.info("request_category_created", category_id=str(category.id), category_key=data.key)
    return RequestCategoryRead.model_validate(category)


async def list_categories() -> list[RequestCategoryRead]:
    """Категории текущего тенанта, по `key` — стабильный порядок для API и UI."""
    async with session_scope() as session:
        categories = await session.scalars(select(RequestCategory).order_by(RequestCategory.key))
        return [RequestCategoryRead.model_validate(category) for category in categories]


async def create_request(
    data: ServiceRequestCreate,
    *,
    request_id: uuid.UUID | None = None,
    acting_user: ActingUser | None = None,
) -> ServiceRequestRead:
    """Создать заявку в статусе `new` и опубликовать `request.created`.

    Категория ищется тенантной сессией: чужая или несуществующая категория
    неразличимы (RLS, P-4) — обе дают ERR-REQUESTS-001.

    `origin` обязателен и умолчания не имеет (spec 0035 §4): из него считается
    Exit-критерий Phase 1, поэтому источник называет каждый создатель заявки
    явно, а не получает молча.

    Заявке присваивается дневной номер `#N`, уникальный в паре
    `(тенант, день отеля)` (issue #38, заход 2а). Защита от гонки — сам
    уникальный индекс: если параллельный создатель занял тот же номер, INSERT
    падает с IntegrityError, номер пересчитывается и попытка повторяется
    (номер не «дырявится» и не дублируется).

    `acting_user` — сотрудник, внёсший заявку формой кабинета: строка журнала
    изменений «Создана вручную» той же транзакцией (spec 0041). None — путь
    без личности (ИИ, HTTP API): журнал не пишется.

    `request_id` — id заявки, выбранный вызывающим заранее (spec 0038 Р-3:
    заказ в номер пишет свой id до заявки). Вызов идемпотентен по нему (P-8):
    заявка с таким id уже есть — она и возвращается, без второй заявки и
    второго события, данные повтора не сравниваются. Параллельный повтор ловит
    нарушение первичного ключа и тоже возвращает существующую. Только для
    вызовов внутри процесса со свежим `uuid4()`: в HTTP-схеме id нет (ревью
    PR #497, Н-5), и id заявки чужого тенанта (RLS её не покажет) пробрасывает
    исходный IntegrityError.
    """
    if request_id is not None:
        existing = await _existing_request(request_id)
        if existing is not None:
            return existing
    for attempt in range(_MAX_DAILY_NUMBER_ATTEMPTS):
        try:
            async with session_scope() as session:
                category = await session.get(RequestCategory, data.category_id)
                if category is None:
                    raise AppError(
                        code=ERR_REQUESTS_CATEGORY_NOT_FOUND,
                        message=f"Request category {data.category_id} does not exist",
                        status_code=404,
                    )
                service_day = await _hotel_service_day(session)
                request = ServiceRequest(
                    id=request_id if request_id is not None else uuid.uuid4(),
                    category_id=category.id,
                    origin=data.origin,
                    summary=data.summary,
                    details=data.details,
                    room_number=data.room_number,
                    guest_language=data.guest_language,
                    is_urgent=data.is_urgent,
                    payment_required=data.payment_required,
                    service_day=service_day,
                    daily_number=await _next_daily_number(session, service_day),
                )
                session.add(request)
                await session.flush()
                if acting_user is not None:
                    record_staff_action(
                        session,
                        acting_user,
                        _request_action(request, category, StaffAction.REQUEST_CREATED),
                    )
                await publish(
                    session,
                    RequestCreated(
                        request_id=request.id, category_id=category.id, summary=data.summary
                    ),
                )
            logger.info(
                "service_request_created",
                request_id=str(request.id),
                category_key=category.key,
                daily_number=request.daily_number,
                is_urgent=request.is_urgent,
                # Не `origin=`: это имя в структурированных логах уже занято
                # HTTP-заголовком Origin (`staff.cross_origin_rejected`,
                # `staff.action_csrf_rejected`), и по нему написано дерево решений
                # в docs/runbooks/errors.md — дежурный фильтрует поток по полю.
                request_origin=data.origin.value,
            )
            return ServiceRequestRead.model_validate(request)
        except IntegrityError as error:
            if request_id is not None and _PRIMARY_KEY_CONSTRAINT in str(error):
                existing = await _existing_request(request_id)
                if existing is None:
                    raise
                return existing
            # Только коллизия дневного номера — ожидаемая гонка, повторяем.
            # Последняя попытка или иной IntegrityError (FK и т.п.) — пробрасываем.
            if (
                _DAILY_NUMBER_CONSTRAINT not in str(error)
                or attempt == _MAX_DAILY_NUMBER_ATTEMPTS - 1
            ):
                raise
            logger.info("daily_number_collision_retry", attempt=attempt + 1)
    raise AssertionError("unreachable: цикл выше либо возвращает, либо пробрасывает")


async def _existing_request(request_id: uuid.UUID) -> ServiceRequestRead | None:
    """Заявка с этим id у текущего тенанта — повтор `create_request` (P-8)."""
    async with session_scope() as session:
        request = await session.get(ServiceRequest, request_id)
        if request is None:
            return None
        logger.info("service_request_create_replayed", request_id=str(request_id))
        return ServiceRequestRead.model_validate(request)


async def _hotel_service_day(session: AsyncSession, /) -> date:
    """Календарный день отеля «сейчас» — база сброса дневного номера (§9).

    День берётся по tz из конфига тенанта (в БД — UTC, локальная дата — слой
    представления). Тенант без конфига (онбординг не завершён; служебный
    smoke-тенант) — деградация на UTC, а не отказ: заявку гостя нумеруем всегда.
    """
    zone: tzinfo = UTC
    try:
        config = await load_tenant_config(session, current_tenant_id())
        zone = config.tzinfo
    except AppError as error:
        if error.code != TENANT_NOT_CONFIGURED_ERROR_CODE:
            raise
        logger.warning("daily_number_service_day_utc_fallback", error_code=error.code)
    return utc_now().astimezone(zone).date()


async def _next_daily_number(session: AsyncSession, service_day: date, /) -> int:
    """Следующий свободный дневной номер: `max(daily_number) + 1` за этот день.

    RLS ограничивает выборку текущим тенантом (P-4), поэтому номер уникален в
    паре `(тенант, день)`. Первое чтение при пустом дне даёт `#1`.
    """
    current_max = await session.scalar(
        select(func.max(ServiceRequest.daily_number)).where(
            ServiceRequest.service_day == service_day
        )
    )
    return (current_max or 0) + 1


async def find_open_requests_by_daily_number(daily_number: int) -> list[ServiceRequestRead]:
    """Незакрытые заявки тенанта с этим дневным номером (резолв команды /done N).

    Номер сбрасывается за сутки: в редкой ситуации незакрытая заявка прошлого
    дня делит `#N` с сегодняшней — тогда список содержит несколько кандидатов, и
    вызывающая сторона (staff.py) просит уточнить. Терминальные (done/cancelled)
    не возвращаются: закрытую заявку номером не трогают. Новые сверху.
    """
    async with session_scope() as session:
        rows = await session.scalars(
            select(ServiceRequest)
            .where(
                ServiceRequest.daily_number == daily_number,
                ServiceRequest.status.in_(_OPEN_STATUSES),
            )
            .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id)
        )
        return [ServiceRequestRead.model_validate(row) for row in rows]


async def list_open_requests_by_ids(request_ids: Sequence[uuid.UUID]) -> list[ServiceRequestRead]:
    """Незакрытые заявки текущего тенанта среди переданных id (spec 0025).

    Опора снапшота «активные заявки диалога»: канал передаёт id из своей
    привязки `request_origins`, домен возвращает те из них, что ещё открыты.
    Чужие тенанту id молча отфильтровывает RLS (P-4), терминальные
    (done/cancelled) не возвращаются — как в `find_open_requests_by_daily_number`.
    Порядок — по времени создания (стабильная нумерация в контексте хода).
    """
    if not request_ids:
        return []
    async with session_scope() as session:
        rows = await session.scalars(
            select(ServiceRequest)
            .where(
                ServiceRequest.id.in_(request_ids),
                ServiceRequest.status.in_(_OPEN_STATUSES),
            )
            .order_by(ServiceRequest.created_at, ServiceRequest.id)
        )
        return [ServiceRequestRead.model_validate(row) for row in rows]


async def list_unclaimed_requests(
    *,
    created_before: datetime,
    limit: int,
    category_ids: Collection[uuid.UUID] | None = None,
) -> list[ServiceRequestRead]:
    """Заявки тенанта, которые никто не взял: `new` и созданы раньше `created_before`.

    Опора напоминаний о невзятых заявках (issue #57, spec 0028). Модуль не знает
    ни про сроки, ни про адресатов: он отвечает на доменный вопрос «что ещё никто
    не взял с такого-то момента», а порог берёт из конфига тенанта вызывающая
    сторона. «Никто не взял» — ровно `new`: переход в `in_progress` и есть
    человеческое «беру» (ADR-013).

    Порядок — новые сверху (как `list_requests`), и это существенно вместе с
    `limit`: срез — страховка от неограниченного скана, и при таком порядке в
    него попадают самые свежие просроченные, то есть те, кому ещё не напоминали.
    При обратном порядке накопившийся хвост давно напомненных висяков однажды
    занял бы весь срез, и новые заявки перестали бы получать напоминания молча.

    `category_ids` — только эти службы (spec 0045 §5: сигнал кабинета режет по
    службам сотрудника в SQL, до среза `limit`); None — все службы тенанта.
    """
    query = select(ServiceRequest).where(
        ServiceRequest.status == RequestStatus.NEW,
        ServiceRequest.created_at < created_before,
    )
    return await _list_scoped(
        query.order_by(ServiceRequest.created_at.desc(), ServiceRequest.id).limit(limit),
        category_ids=category_ids,
    )


async def list_open_requests(
    *,
    limit: int,
    category_ids: Collection[uuid.UUID] | None = None,
    claimed_by_user_id: uuid.UUID | None = None,
) -> list[ServiceRequestRead]:
    """Открытые заявки тенанта (`new` + `in_progress`), новые сверху.

    Лента очереди кабинета персонала (spec 0033 §5). `category_ids` — только эти
    службы (None — все), `claimed_by_user_id` — только взятые этим сотрудником
    («мои»). Оба фильтра — в SQL ДО среза `limit` (spec 0045 §5, #221 п. 2): в
    памяти после среза вкладка службы при длинной общей очереди молча
    показывала бы подмножество. `limit` — страховка от неограниченного скана
    (канон `list_unclaimed_requests`): при аномальном хвосте в срез попадают
    самые свежие — те, с которыми персонал работает.
    """
    query = select(ServiceRequest).where(ServiceRequest.status.in_(_OPEN_STATUSES))
    if claimed_by_user_id is not None:
        query = query.where(ServiceRequest.claimed_by_user_id == claimed_by_user_id)
    return await _list_scoped(
        query.order_by(ServiceRequest.created_at.desc(), ServiceRequest.id).limit(limit),
        category_ids=category_ids,
    )


async def list_requests_closed_since(
    *,
    closed_after: datetime,
    limit: int,
    category_ids: Collection[uuid.UUID] | None = None,
    claimed_by_user_id: uuid.UUID | None = None,
) -> list[ServiceRequestRead]:
    """Закрытые заявки тенанта (`done`/`cancelled`) с `closed_at >= closed_after`.

    Вкладка «закрытые за сегодня» кабинета (spec 0033 §5): границу суток
    (полночь отеля) считает вызывающая сторона — модуль не знает про часовые
    пояса представления, как в `list_unclaimed_requests`. Момент закрытия
    берётся из `closed_at` (spec 0035 §3), а не угадывается по `updated_at`:
    обезличивание ретеншна (#42) бампает `updated_at` (onupdate), и без
    `closed_at` в день прогона джобы вкладка показывала бы пачку древних
    заявок — это чинилось отдельным условием по плейсхолдеру (ревью PR #154).
    Теперь условие не нужно: обезличивание меняет текст, а не момент закрытия.

    Фильтр по статусу остаётся: «заявка закрыта» — это статус, а не
    `closed_at IS NOT NULL` (§3). У терминальных строк, обезличенных ДО
    миграции 0025, `closed_at` пуст навсегда — сравнение с NULL их и
    отфильтровывает, отдельного условия не требуя.

    Следствие принято намеренно: заявка, провисевшая открытой дольше срока
    ретеншна и закрытая сегодня, попадёт во вкладку с плейсхолдером вместо
    текста (джоба #42 обезличивает по возрасту, статус ей не важен). Старое
    условие по плейсхолдеру прятало её целиком — то есть скрывало закрытие,
    которое действительно случилось сегодня, и расходилось бы со сводкой дня
    (spec 0035 §6, она считает закрытия по `closed_at`). Показать строку без
    текста честнее: категория, комната и номер `#N` на карточке остаются, а
    «сегодня» задаёт сама вкладка — времени закрытия карточка не показывает
    (единственная её временная подпись — возраст от создания, `staff_portal/queue.py`).
    Свежезакрытые сверху; `limit` — та же страховка от скана. Фильтры
    `category_ids` и `claimed_by_user_id` — как у `list_open_requests`, в SQL.
    """
    query = select(ServiceRequest).where(
        ServiceRequest.status.not_in(_OPEN_STATUSES),
        ServiceRequest.closed_at >= closed_after,
    )
    if claimed_by_user_id is not None:
        query = query.where(ServiceRequest.claimed_by_user_id == claimed_by_user_id)
    return await _list_scoped(
        query.order_by(ServiceRequest.closed_at.desc(), ServiceRequest.id).limit(limit),
        category_ids=category_ids,
    )


async def _list_scoped(
    query: Select[tuple[ServiceRequest]], *, category_ids: Collection[uuid.UUID] | None
) -> list[ServiceRequestRead]:
    """Выполнить выборку ленты, срезанную по службам (spec 0045 §5).

    Пустой набор служб — пустой ответ без запроса: сотруднику без служб
    показывать нечего, а `IN ()` у разных драйверов ведёт себя по-разному.
    """
    if category_ids is not None:
        if not category_ids:
            return []
        query = query.where(ServiceRequest.category_id.in_(list(category_ids)))
    async with session_scope() as session:
        rows = await session.scalars(query)
        return [ServiceRequestRead.model_validate(row) for row in rows]


async def list_requests(*, limit: int, offset: int) -> ServiceRequestPage:
    """Страница заявок текущего тенанта, новые сверху (канон пагинации Task 0013).

    Границы limit/offset валидирует HTTP-слой (Query в router.py); сервис
    доверяет вызывающей стороне внутри процесса. Сортировка стабильна:
    `created_at DESC` с tie-break по `id` — страницы не перекрываются
    при равных временах создания.
    """
    async with session_scope() as session:
        total = await session.scalar(select(func.count()).select_from(ServiceRequest))
        rows = await session.scalars(
            select(ServiceRequest)
            .order_by(ServiceRequest.created_at.desc(), ServiceRequest.id)
            .limit(limit)
            .offset(offset)
        )
        items = [ServiceRequestRead.model_validate(row) for row in rows]
    return ServiceRequestPage(items=items, total=total or 0, limit=limit, offset=offset)


async def change_request_status(
    request_id: uuid.UUID,
    new_status: RequestStatus,
    *,
    resolution_note: str | None = None,
    initiator: RequestInitiator | None = None,
    acting_user: ActingUser | None = None,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
    only_from: RequestStatus | None = None,
) -> ServiceRequestRead:
    """Перевести заявку в новый статус и опубликовать `request.status_changed`.

    Допустимые переходы — `STATUS_TRANSITIONS`; недопустимый (в том числе
    из терминального статуса или в тот же самый) — ERR-REQUESTS-003.

    `resolution_note` — примечание персонала к закрытию (spec 0021 П-4:
    частичное выполнение / причина отмены). Пишется только на терминальном
    переходе (done/cancelled); на прочих игнорируется с warning-логом —
    примечание не расширяет карту переходов.

    `initiator` — кто инициировал переход (spec 0025); уезжает в событие как
    есть. None — не указан: так работают существующие пути персонала, их
    уведомления не меняются.

    `acting_user` — кто действует из кабинета (spec 0033 §5): на переходе
    new → in_progress («взять») заполняет `claimed_by_*`, на терминальном
    («выполнено»/«отменить») — `closed_by_*`. None — путь без личности
    (Telegram-суррогат, HTTP API, отмена гостем): имена остаются пустыми.

    Возврат в очередь (in_progress → new, spec 0042 §2.1) обнуляет всю пару
    взятия — и время, и имя, — с личностью или без.

    **Время пишется всегда, имя — только с личностью** (spec 0035 §3, главная
    асимметрия этой функции). `claimed_at` ставится на КАЖДОМ переходе
    new → in_progress, `closed_at` — на КАЖДОМ терминальном, из любого канала:
    медиана времени взятия, посчитанная только по кабинету, молча занизила бы
    объём работы, сделанной через Telegram, а на пилоте оба пути живут
    одновременно (spec 0033 §8). Отсюда предикат, который обязана сохранить
    любая будущая правка карты переходов (возврат в new снимает обе половины
    пары разом, `_release_claim`):
    `claimed_by_user_id IS NOT NULL` ⟹ `claimed_at IS NOT NULL`, и то же для
    пары закрытия; обратное неверно — «взято, но некем» это норма данных.
    Перезаписи `closed_at` не бывает: терминальный переход в этой модели
    последний.

    С личностью переход пишет и строку журнала изменений (spec 0041) той же
    транзакцией: отказ перехода (ERR-REQUESTS-003) строки не оставляет.

    `allowed_category_ids` — службы, над заявками которых действующий вправе
    работать (spec 0045 §5); None — любые (ресепшен, менеджер, пути без
    личности). Заявка другой службы — ERR-REQUESTS-007 (403) раньше проверки
    перехода: о чужой заявке отказ не рассказывает, в каком она статусе.

    `only_from` — переход, только если статус сейчас ровно такой, иначе
    ERR-REQUESTS-003 (spec 0038 §4). Сверяется под той же блокировкой: из
    «гость отменяет невзятый заказ» и «сотрудник его берёт» выигрывает один —
    без условия отмена прошла бы и по уже взятой заявке.

    Оплата при выполнении (spec 0038 Р-2) проверяется здесь, а не у вызывающих:
    заявку с признаком `payment_required` без отметки «Оплачено» не закрыть
    `done` ни из одного канала (ERR-REQUESTS-006), а отмену оплаченной гостем
    (`initiator=guest`) домен отвергает (ERR-REQUESTS-008) — деньги уже у
    сотрудника. Отмену оплаченной сотрудником домен пропускает: возврат денег —
    касса отеля.
    """
    async with session_scope() as session:
        # FOR UPDATE: конкурентная смена статуса той же заявки валидируется
        # по актуальному значению, а не по прочитанному до чужого commit'а.
        # Служба проверяется под той же блокировкой: заявку, переданную за
        # секунду до нажатия, прежняя служба уже не закроет.
        request = await _get_request_or_raise(session, request_id, for_update=True)
        _ensure_own_service(request, allowed_category_ids)
        old_status = request.status
        if new_status not in STATUS_TRANSITIONS[old_status] or (
            only_from is not None and old_status is not only_from
        ):
            raise AppError(
                code=ERR_REQUESTS_INVALID_STATUS_TRANSITION,
                message=(
                    f"Cannot change request status from {old_status.value!r} "
                    f"to {new_status.value!r}"
                    + (f" (only from {only_from.value!r})" if only_from is not None else "")
                ),
                status_code=409,
            )
        _ensure_payment_allows(request, new_status, initiator)
        request.status = new_status
        now = utc_now()
        is_terminal = new_status not in _OPEN_STATUSES
        if old_status is RequestStatus.NEW and new_status is RequestStatus.IN_PROGRESS:
            request.claimed_at = now
            if acting_user is not None:
                request.claimed_by_user_id = acting_user.user_id
                request.claimed_by_display_name = acting_user.display_name
        if new_status is RequestStatus.NEW:
            _release_claim(request)
        if is_terminal:
            request.closed_at = now
            if acting_user is not None:
                request.closed_by_user_id = acting_user.user_id
                request.closed_by_display_name = acting_user.display_name
        if resolution_note is not None:
            if not is_terminal:
                # Нетерминальный переход: примечанию некуда «закрыться» — игнор.
                logger.warning(
                    "resolution_note_ignored_non_terminal",
                    request_id=str(request.id),
                    new_status=new_status.value,
                )
            else:
                request.resolution_note = resolution_note.strip() or None
        if acting_user is not None:
            category = await session.get(RequestCategory, request.category_id)
            record = _request_action(request, category, _STATUS_ACTIONS[new_status])
            # Примечание к закрытию было — но сам текст в журнал не идёт (§2.1).
            record.with_note = is_terminal and request.resolution_note is not None
            record_staff_action(session, acting_user, record)
        await publish(
            session,
            RequestStatusChanged(
                request_id=request.id,
                old_status=old_status,
                new_status=new_status,
                initiator=initiator,
            ),
        )
    logger.info(
        "service_request_status_changed",
        request_id=str(request.id),
        old_status=old_status.value,
        new_status=new_status.value,
    )
    return ServiceRequestRead.model_validate(request)


def _ensure_payment_allows(
    request: ServiceRequest, new_status: RequestStatus, initiator: RequestInitiator | None
) -> None:
    """Переход не нарушает оплату при выполнении (spec 0038 Р-2) — иначе 006/008."""
    if new_status is RequestStatus.DONE and request.payment_required and request.paid_at is None:
        logger.warning("service_request_done_without_payment_rejected", request_id=str(request.id))
        raise AppError(
            code=ERR_REQUESTS_DONE_WITHOUT_PAYMENT,
            message="Service request requires a payment mark before it can be done",
            status_code=409,
        )
    if (
        new_status is RequestStatus.CANCELLED
        and initiator is RequestInitiator.GUEST
        and request.paid_at is not None
    ):
        logger.warning("service_request_paid_guest_cancel_rejected", request_id=str(request.id))
        raise AppError(
            code=ERR_REQUESTS_PAYMENT_NOT_APPLICABLE,
            message="A paid service request cannot be cancelled by the guest",
            status_code=409,
        )


def _release_claim(request: ServiceRequest) -> None:
    """Вернуть заявку в очередь: `new` и пустая пара взятия (spec 0042 §2.1).

    Время и имя снимаются вместе (spec 0035 §3): «взята в 06:10» у заявки,
    которую никто не делает, — неправда в карточке и в сводке дня. `created_at`
    не трогается: гость ждёт с момента просьбы, а не с момента возврата.
    """
    request.status = RequestStatus.NEW
    request.claimed_at = None
    request.claimed_by_user_id = None
    request.claimed_by_display_name = None


async def transfer_request(
    request_id: uuid.UUID,
    category_id: uuid.UUID,
    *,
    acting_user: ActingUser | None = None,
    allowed_category_ids: Collection[uuid.UUID] | None = None,
) -> ServiceRequestRead:
    """Передать открытую заявку в другую службу (spec 0042 §2.2).

    Взятая заявка возвращается в очередь новой службы (`_release_claim`): её
    взял сотрудник прежней службы, а делать будет другая. Публикуется
    `request.category_changed`, а у взятой — ещё и `request.status_changed`
    (`in_progress → new`) той же транзакцией: переход статуса без события сломал
    бы контракт «по событию на каждый переход».

    Чужая или несуществующая служба — ERR-REQUESTS-001 (RLS, как в
    `create_request`); закрытая заявка или та же служба — ERR-REQUESTS-005.
    Та же служба на деле — гонка двух передавших. С `acting_user` — одна строка
    журнала (откуда → куда), даже если заявка заодно вернулась в очередь:
    сотрудник сделал одно действие.

    `allowed_category_ids` ограничивает службу, ОТКУДА передают (как у
    `change_request_status`, ERR-REQUESTS-007); куда — любая служба отеля
    (spec 0045 §3).
    """
    async with session_scope() as session:
        request = await _get_request_or_raise(session, request_id, for_update=True)
        _ensure_own_service(request, allowed_category_ids)
        target = await session.get(RequestCategory, category_id)
        if target is None:
            raise AppError(
                code=ERR_REQUESTS_CATEGORY_NOT_FOUND,
                message=f"Request category {category_id} does not exist",
                status_code=404,
            )
        old_status = request.status
        old_category_id = request.category_id
        if old_status not in _OPEN_STATUSES or old_category_id == target.id:
            raise AppError(
                code=ERR_REQUESTS_TRANSFER_REJECTED,
                message=(
                    f"Cannot transfer request in status {old_status.value!r} "
                    f"to category {target.id}"
                ),
                status_code=409,
            )
        source = await session.get(RequestCategory, old_category_id)
        request.category_id = target.id
        if old_status is not RequestStatus.NEW:
            _release_claim(request)
        if acting_user is not None:
            record = _request_action(request, source, StaffAction.REQUEST_TRANSFERRED)
            record.category_name_to = target.name
            record.category_id_to = target.id
            record_staff_action(session, acting_user, record)
        await publish(
            session,
            RequestCategoryChanged(
                request_id=request.id,
                old_category_id=old_category_id,
                new_category_id=target.id,
            ),
        )
        if old_status is not RequestStatus.NEW:
            await publish(
                session,
                RequestStatusChanged(
                    request_id=request.id,
                    old_status=old_status,
                    new_status=RequestStatus.NEW,
                    initiator=RequestInitiator.STAFF,
                ),
            )
    logger.info(
        "service_request_transferred",
        request_id=str(request.id),
        old_category_key=source.key if source is not None else None,
        new_category_key=target.key,
        returned_to_queue=old_status is not RequestStatus.NEW,
    )
    return ServiceRequestRead.model_validate(request)


def _ensure_own_service(
    request: ServiceRequest, allowed_category_ids: Collection[uuid.UUID] | None
) -> None:
    """Заявка в одной из служб действующего — иначе ERR-REQUESTS-007 (403)."""
    if allowed_category_ids is None or request.category_id in allowed_category_ids:
        return
    logger.warning(
        "service_request_foreign_service_rejected",
        request_id=str(request.id),
        category_id=str(request.category_id),
    )
    raise AppError(
        code=ERR_REQUESTS_FOREIGN_SERVICE,
        message="Service request belongs to a service outside the actor's services",
        status_code=403,
    )


def _request_action(
    request: ServiceRequest, category: RequestCategory | None, action: StaffAction
) -> StaffActionRecord:
    """Снапшот заявки для строки журнала: `#N`, его день, служба, комната —
    без единого слова из текста заявки (spec 0041 §2.1).

    Служба — из переданной категории, а не из `request.category_id`: у передачи
    заявка к этому моменту уже в новой службе, а строка пишет, откуда её
    передали. По `category_id` старший видит строки своих служб (spec 0045 §3).
    """
    return StaffActionRecord(
        action=action,
        request_id=request.id,
        request_daily_number=request.daily_number,
        request_service_day=request.service_day,
        category_name=category.name if category is not None else None,
        category_id=category.id if category is not None else None,
        room_number=request.room_number,
    )


async def get_request(request_id: uuid.UUID) -> ServiceRequestRead:
    """Прочитать заявку текущего тенанта; чужая или несуществующая — ERR-REQUESTS-002."""
    async with session_scope() as session:
        request = await _get_request_or_raise(session, request_id)
    return ServiceRequestRead.model_validate(request)


async def _get_request_or_raise(
    session: AsyncSession, request_id: uuid.UUID, *, for_update: bool = False
) -> ServiceRequest:
    query = select(ServiceRequest).where(ServiceRequest.id == request_id)
    if for_update:
        query = query.with_for_update()
    request = (await session.execute(query)).scalar_one_or_none()
    if request is None:
        raise AppError(
            code=ERR_REQUESTS_REQUEST_NOT_FOUND,
            message=f"Service request {request_id} does not exist",
            status_code=404,
        )
    return request


async def anonymize_expired_request_texts(*, created_before: datetime) -> int:
    """Обезличить свободный текст заявок старше `created_before` (issue #42).

    Часть ретеншна гостевых текстов (spec 0032 §2): `summary` заменяется
    плейсхолдером, `details` и `resolution_note` обнуляются; агрегатные поля
    (статус, категория, комната, номер, времена) не трогаются — отчёты живут.
    Статус заявки не важен: обещание политики конфиденциальности («свободный
    текст заявок обезличивается через 90 дней») сильнее удобства персонала.

    Идемпотентна (P-8): уже обезличенные строки отфильтрованы условием, повторный
    прогон обновляет 0 строк. Вызывается композиционным слоем
    (`channels/common/retention.py`) внутри `tenant_context` каждого тенанта.
    """
    async with session_scope() as session:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(ServiceRequest)
                .where(
                    ServiceRequest.created_at < created_before,
                    or_(
                        ServiceRequest.summary != REQUEST_TEXT_ANONYMIZED_PLACEHOLDER,
                        ServiceRequest.details.is_not(None),
                        ServiceRequest.resolution_note.is_not(None),
                    ),
                )
                .values(
                    summary=REQUEST_TEXT_ANONYMIZED_PLACEHOLDER,
                    details=None,
                    resolution_note=None,
                )
            ),
        )
    anonymized = result.rowcount or 0
    if anonymized:
        logger.info("service_request_texts_anonymized", anonymized=anonymized)
    return anonymized
