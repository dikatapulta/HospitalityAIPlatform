"""Доска «Этажи» (spec 0053 §5, issue #660): какие этажи на доске, подписи и время раздачи.

Страница целиком — `test_floors.py`; здесь то, что добавила доска: этажи подряд от
нижнего до верхнего известного (раздача и номера с действующим проживанием),
подпись горничной в списке и «2 дня назад» у времени раздачи.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from hospitality.platform.models import StaffRole
from hospitality.shared.config import get_settings
from hospitality.staff_portal import floors_view
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login
from hospitality.staff_portal.tests.test_floors import _member, _save
from hospitality.staff_portal.tests.test_home import _check_in
from hospitality.staff_portal.tests.test_turnovers import HOUSEKEEPING, _categories

PAGE = f"/staff/{HOTEL_SLUG}/floors"
ALMATY = ZoneInfo("Asia/Almaty")
# Разметка доски кончается шаблонами карточки и этажа для floors.js.
CARD_TEMPLATE = '<template id="floor-card-template">'


@pytest.fixture(autouse=True)
def _floors_enabled() -> Iterator[None]:
    patcher = pytest.MonkeyPatch()
    patcher.setenv("ENABLE_HOUSEKEEPING_FLOORS", "true")
    get_settings.cache_clear()
    yield
    patcher.undo()
    get_settings.cache_clear()


def test_floors_line_names_one_many_and_none() -> None:
    assert floors_view.floors_line(frozenset()) == "без этажа"
    assert floors_view.floors_line(frozenset({7})) == "этаж 7"
    assert floors_view.floors_line(frozenset({8, 7, 12, 13, 14})) == "этажи 7, 8, 12-14"


def test_board_floors_fill_the_gap_between_known() -> None:
    assert floors_view.board_floors([]) == []
    assert floors_view.board_floors([9]) == [9]
    assert floors_view.board_floors({12, 7, 9}) == [7, 8, 9, 10, 11, 12]


@pytest.mark.parametrize(
    ("assigned", "line"),
    [
        (datetime(2026, 11, 2, 3, 15, tzinfo=UTC), "Раздано сегодня в 08:15"),
        (datetime(2026, 11, 1, 3, 15, tzinfo=UTC), "Раздано 01.11 в 08:15 · вчера"),
        (datetime(2026, 10, 31, 3, 15, tzinfo=UTC), "Раздано 31.10 в 08:15 · 2 дня назад"),
        (datetime(2026, 10, 28, 3, 15, tzinfo=UTC), "Раздано 28.10 в 08:15 · 5 дней назад"),
        (datetime(2026, 10, 12, 3, 15, tzinfo=UTC), "Раздано 12.10 в 08:15 · 21 день назад"),
        # По UTC — ещё 31.10, в Алматы — уже 01:00 1-го: дни — по поясу отеля.
        (datetime(2026, 10, 31, 20, 0, tzinfo=UTC), "Раздано 01.11 в 01:00 · вчера"),
        (datetime(2026, 11, 3, 3, 15, tzinfo=UTC), "Раздано 03.11 в 08:15"),  # часы разошлись
    ],
)
def test_distributed_line_counts_days_in_hotel_zone(assigned: datetime, line: str) -> None:
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)  # 17:00 по Алматы
    assert floors_view.distributed_line(assigned, ALMATY, now) == line


async def test_board_draws_floors_of_distribution_and_active_stays(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Раздан 7-й, гости живут в 1204 и «Люкс» — доска 7…12: номер без этажа не
    добавляет ничего, пустые 8–11 нарисованы «никем»."""
    await _categories(portal_hotel)
    aigul, _ = await _member(portal_hotel, StaffRole.STAFF, HOUSEKEEPING, "Айгуль")
    await submit_login(client, portal_hotel.login)
    await _save(client, [(aigul, "7")])
    await _check_in(portal_hotel.tenant_id, "1204")
    await _check_in(portal_hotel.tenant_id, "Люкс")

    board = (await client.get(PAGE)).text.split(CARD_TEMPLATE)[0]

    drawn = [floor for floor in range(1, 100) if f'"floor-cell" data-floor="{floor}"' in board]
    assert drawn == [7, 8, 9, 10, 11, 12]
    assert board.count("Никого — заявки слышит вся служба</p>") == 6
    assert board.count('class="floor-empty" hidden>') == 1  # на 7-м — Айгуль
    assert 'id="floors-board-empty" class="hint" hidden>' in board
