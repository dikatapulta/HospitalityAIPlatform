"""Куда сотруднику писать о сбое (#1055): подвал каждой страницы кабинета — и до входа,
и в каркасе отеля — и строка на листке приглашений. Почта — одно место,
`rendering.SUPPORT_EMAIL`."""

from __future__ import annotations

from httpx import AsyncClient

from hospitality.staff_portal.rendering import SUPPORT_EMAIL
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel, submit_login

FOOT = (
    '<footer class="page-foot">Что-то не работает? Поддержка кабинета:\n'
    f'  <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></footer>'
)


def test_support_is_the_operator_mailbox_named_in_the_staff_notice() -> None:
    """Ящик Zoho уже назван в уведомлении персоналу и в DPA N.3 — другой адрес или
    канал сначала туда (docs/legal/staff-notice.md, «Кто ещё участвует в обработке»)."""
    assert SUPPORT_EMAIL == "support@necturn.com"


async def test_entry_pages_carry_the_support_footer(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """До входа подвал нужнее всего: не открылся вход — писать больше некуда."""
    for path in ("/staff/login", f"/staff/{HOTEL_SLUG}/login", "/staff/invite/no-such-token"):
        response = await client.get(path)
        assert response.status_code in (200, 410), path
        assert response.text.count(FOOT) == 1, path


async def test_hotel_pages_carry_the_support_footer(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    await submit_login(client, portal_hotel.login)

    for path in (f"/staff/{HOTEL_SLUG}", f"/staff/{HOTEL_SLUG}/team"):
        page = (await client.get(path)).text
        assert 'class="app-side"' in page  # каркас отеля, не страница входа
        assert page.count(FOOT) == 1, path


async def test_invite_card_names_support_beside_the_qr(
    client: AsyncClient, portal_hotel: PortalHotel
) -> None:
    """Строка — в одном ряду с QR: ниже карточка выросла бы, и шесть на A4 не легли бы."""
    await submit_login(client, portal_hotel.login)

    page = (await client.get(f"/staff/{HOTEL_SLUG}/team")).text

    card = page.split("<template data-sheet-card>", 1)[1].split("</template>", 1)[0]
    row = card.split('<div class="invite-card-qr-row">', 1)[1].split("</div>\n", 2)
    assert "data-card-qr" in row[0]
    assert f"Не получается войти? Поддержка кабинета: {SUPPORT_EMAIL}</p>" in row[1]
