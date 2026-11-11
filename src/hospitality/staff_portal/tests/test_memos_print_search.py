"""«Служебные записки» — печать и поиск реестра (spec 0077 §5, §9, §12 PR 4б-3; #936).

Печать: только отправленной записи и только участнику; лист подписей, решение и строка
§9 внизу; просмотры на бумагу не идут, а открытие печати само пишется в журнал. Поиск:
номер или заголовок, имя участника (и подписанта), вид, статус, даты по поясу отеля;
неизвестное значение условия — как пустое; посторонний чужого не находит.
"""

# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from httpx import AsyncClient

from hospitality.modules.memos import api as memos_api
from hospitality.platform.models import StaffRole
from hospitality.staff_portal import memos_search
from hospitality.staff_portal.tests.conftest import PortalHotel
from hospitality.staff_portal.tests.test_memos_screens import (
    API,
    MEMOS,
    SAME_ORIGIN,
    TITLE,
    _confirmed,
    _draft,
    _hotel,
    _member,
    app,  # noqa: F401  (фикстура для pytest)
    shared_redis,  # noqa: F401  (фикстура для pytest)
    stand,  # noqa: F401  (фикстура для pytest)
)

OTHER_TITLE = "Ремонт кондиционера"
FOOTER = "Сформировано в Necturn"


async def _sent_without_signers(client: AsyncClient, boss: uuid.UUID, title: str) -> str:
    created = await client.post(
        f"{API}/drafts",
        json={"title": title, "body": "Шумит в 412.", "addressee_id": str(boss), "signer_ids": []},
        headers=SAME_ORIGIN,
    )
    assert created.status_code == 200, created.text
    memo_id: str = created.json()["id"]
    assert await _confirmed(client, memo_id, {"kind": "send"}) == "submitted"
    return memo_id


async def test_print_page(stand: dict[str, AsyncClient], portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    author, signer, boss, stranger = (
        stand[who] for who in ("author", "signer", "boss", "stranger")
    )
    await _member(author, portal_hotel, "Айгуль Автор")
    signer_id = await _member(signer, portal_hotel, "Дана Подписант")
    boss_id = await _member(boss, portal_hotel, "Ерлан Старший", StaffRole.SENIOR)
    await _member(stranger, portal_hotel, "Посторонний")

    memo_id = await _draft(author, boss_id, [signer_id])
    assert (await author.get(f"{MEMOS}/{memo_id}/print")).status_code == 404  # черновик
    assert f"/{memo_id}/print" not in (await author.get(f"{MEMOS}/{memo_id}")).text

    assert await _confirmed(author, memo_id, {"kind": "send"}) == "signing"
    assert await _confirmed(signer, memo_id, {"kind": "sign"}) == "signing"
    submitted = await author.post(f"{API}/{memo_id}/submit", json={}, headers=SAME_ORIGIN)
    assert submitted.status_code == 200, submitted.text
    decided = {"kind": "decide", "decision": "approved", "comment": "Закупить до пятницы"}
    assert await _confirmed(boss, memo_id, decided) == "approved"

    assert f"/{memo_id}/print" in (await author.get(f"{MEMOS}/{memo_id}")).text
    assert (await stranger.get(f"{MEMOS}/{memo_id}/print")).status_code == 404

    page = await boss.get(f"{MEMOS}/{memo_id}/print")
    assert page.status_code == 200
    for expected in (
        "Служебная записка СЗ-",
        TITLE,
        "Прошу закупить 40 полотенец.",
        "Решение: Согласовано — Ерлан Старший",
        "Закупить до пятницы",
        "Лист подписей",
        "Подписал(а)",
        "Дана Подписант",
        "подтверждено паролем кабинета",
        "Печать / сохранить в PDF",
        f"{FOOTER} ",
        "запись не является электронным документом с ЭЦП.",
        'class="memo-sample"',
    ):
        assert expected in page.text, expected
    assert "Просмотры" not in page.text
    # Руководитель решил по API и карточку не открывал: его просмотр — от печати.
    assert "Ерлан Старший —" in (await author.get(f"{MEMOS}/{memo_id}")).text

    await _hotel(portal_hotel.tenant_id, enabled=False)
    assert (await author.get(f"{MEMOS}/{memo_id}/print")).status_code == 404


async def test_registry_search(stand: dict[str, AsyncClient], portal_hotel: PortalHotel) -> None:
    await _hotel(portal_hotel.tenant_id)
    author, signer, stranger = stand["author"], stand["signer"], stand["stranger"]
    await _member(author, portal_hotel, "Айгуль Автор")
    signer_id = await _member(signer, portal_hotel, "Дана Подписант")
    await _member(stranger, portal_hotel, "Посторонний")
    first = await _draft(author, portal_hotel.user_id, [signer_id])
    assert await _confirmed(author, first, {"kind": "send"}) == "signing"
    await _sent_without_signers(author, portal_hotel.user_id, OTHER_TITLE)

    async def found(query: str, client: AsyncClient = author) -> str:
        response = await client.get(f"{MEMOS}/all?{query}")
        assert response.status_code == 200
        return response.text

    plain = await found("")
    assert TITLE in plain and OTHER_TITLE in plain and "Найдено" not in plain
    by_title = await found("q=ПОЛОТЕН")
    assert TITLE in by_title and OTHER_TITLE not in by_title and "Найдено: 1" in by_title
    assert " open>" in by_title  # блок поиска раскрыт, когда условие задано
    by_signer = await found("who=дана")
    assert TITLE in by_signer and OTHER_TITLE not in by_signer
    by_status = await found("status=submitted")
    assert OTHER_TITLE in by_status and TITLE not in by_status
    nothing = await found("since=2000-01-01&until=2000-01-02")
    assert TITLE not in nothing and "Ничего не нашлось" in nothing
    bogus = await found("status=bogus&kind=x&since=вчера")
    assert TITLE in bogus and OTHER_TITLE in bogus
    assert TITLE not in await found("q=полотен", stranger)


def _summary(**fields: object) -> memos_api.MemoSummary:
    base: dict[str, object] = {
        "id": uuid.uuid4(),
        "kind": memos_api.MemoKind.MEMO,
        "number": "СЗ-2026/14",
        "status": memos_api.MemoStatus.SIGNING,
        "title": "Закупка полотенец",
        "author_id": uuid.uuid4(),
        "addressee_id": uuid.uuid4(),
        "created_at": datetime(2026, 11, 9, 4, 0, tzinfo=UTC),
        # 01:00 11.11 по Алматы — накануне по UTC.
        "sent_at": datetime(2026, 11, 10, 20, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 11, 10, 20, 0, tzinfo=UTC),
        "signer_ids": [],
    }
    return memos_api.MemoSummary.model_validate({**base, **fields})


def test_filter_conditions() -> None:
    zone = ZoneInfo("Asia/Almaty")
    signer = uuid.uuid4()
    memo = _summary(signer_ids=[signer])
    names = {signer: "Дана Подписант"}

    def hit(**params: str) -> bool:
        condition = memos_search.parse_filter(params)
        return memos_search.matches(memo, condition, lambda user: names.get(user, "?"), zone)

    assert hit() and not memos_search.parse_filter({}).active
    assert hit(q="сз-2026/14") and hit(q="полотенец") and not hit(q="кондиционер")
    assert hit(who="ДАНА") and not hit(who="Ерлан")
    assert hit(kind="memo") and not hit(kind="explanation") and hit(kind="нет такого")
    assert hit(status="signing") and not hit(status="approved")
    # Дата — по поясу отеля: отправлена 11.11 по Алматы, хотя по UTC ещё 10.11.
    assert hit(since="2026-11-11", until="2026-11-11")
    assert not hit(until="2026-11-10") and not hit(since="2026-11-12")
    assert hit(since="не дата")
    draft = _summary(number=None, sent_at=None, status=memos_api.MemoStatus.DRAFT)
    by_created = memos_search.parse_filter({"until": "2026-11-09"})
    assert memos_search.matches(draft, by_created, lambda user: "?", zone)
    long = memos_search.parse_filter({"q": "я" * 500})
    assert len(long.text) == memos_search.QUERY_MAX_CHARS
    assert memos_search.parse_filter({"since": "2026-11-01"}).since == date(2026, 11, 1)
