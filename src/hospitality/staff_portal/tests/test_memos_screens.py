"""«Служебные записки» — экраны записок (spec 0077 §3, §5, §9, §12 PR 4б-2; #936).

Ход записки по HTTP тремя входами: автор пишет черновик и отправляет с подтверждением
паролем, подписант видит её в «Ждут меня» и числом в меню и подписывает, автор отдаёт
руководителю, тот возвращает на доработку, автор создаёт новую на основе. Пуши — без
заголовка записки. Посторонний — 404 на карточке и действии; раздел выключен — 404.
Лист подписей называет особые случаи ключа: ключ потом отвязал новый пароль и ключ
привязан после сброса пароля (ревью PR #977, later pr955).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.modules.memos import api as memos_api
from hospitality.platform import staff_push
from hospitality.platform.config import HotelProfile, TenantConfig, store_tenant_config
from hospitality.platform.models import StaffRole
from hospitality.platform.staff_confirm import ConfirmationMethod, ProofStatus
from hospitality.platform.staff_password_reset import accept_password_reset
from hospitality.platform.staff_superadmin import issue_password_reset
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.ports.web_push import (
    PushDelivery,
    PushOutcome,
    PushTarget,
    b64url_encode,
    set_web_push,
    web_push_or_none,
)
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import memos_card
from hospitality.staff_portal import memos_texts as texts
from hospitality.staff_portal.tests.conftest import HOTEL_SLUG, PortalHotel
from tests.conftest import TEST_FILE_ENCRYPTION_KEY, FakeRateLimitRedis
from tests.passkey_authenticator import IPHONE_SAFARI_UA, SoftAuthenticator
from tests.test_staff_auth import PASSWORD, create_staff_user, unique_login

HOME = f"/staff/{HOTEL_SLUG}"
MEMOS = f"{HOME}/memos"
API = f"{HOME}/api/memos"
SAME_ORIGIN = {"origin": "https://test"}
PHONE = {**SAME_ORIGIN, "user-agent": IPHONE_SAFARI_UA}
STROKES = [[[10, 20], [300, 200], [600, 120]]]
TITLE = "Закупка полотенец"
NEW_PASSWORD = "new-horse-battery-staple"


@pytest.fixture
def shared_redis() -> Iterator[FakeRateLimitRedis]:
    """Один фейк Redis на тест: вызов подтверждения живёт между двумя запросами;
    тестовый ключ шифрования — тексты записок модуль хранит шифртекстом."""
    fake = FakeRateLimitRedis()
    patcher = pytest.MonkeyPatch()
    patcher.setattr("hospitality.shared.ratelimit.create_redis_client", lambda: fake)
    patcher.setenv("FILE_ENCRYPTION_KEY", TEST_FILE_ENCRYPTION_KEY)
    get_settings.cache_clear()
    yield fake
    patcher.undo()
    get_settings.cache_clear()


class RecordingPush:
    """Двойник порта пуша: кабинету импорт `integrations` запрещён (контракт P-3), а
    тесту нужно только «что ушло и куда» — тело без шифрования."""

    backend = "recording"
    application_server_key = "test"

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    async def send(self, target: PushTarget, payload: bytes, *, ttl_seconds: int) -> PushDelivery:
        self.sent.append((target.endpoint, payload))
        return PushDelivery(PushOutcome.DELIVERED, 201)

    async def check(self) -> bool:
        return True


@pytest.fixture
def fake_push() -> Iterator[RecordingPush]:
    previous = web_push_or_none()
    adapter = RecordingPush()
    set_web_push(adapter)
    yield adapter
    set_web_push(previous)


@pytest.fixture
async def app(shared_redis: FakeRateLimitRedis) -> FastAPI:
    return create_app()


@pytest.fixture
async def stand(app: FastAPI) -> AsyncIterator[dict[str, AsyncClient]]:
    """Браузер на каждого участника — своя банка cookie."""
    clients = {
        name: AsyncClient(transport=ASGITransport(app=app), base_url="https://test")
        for name in ("author", "signer", "boss", "stranger")
    }
    yield clients
    for client in clients.values():
        await client.aclose()


async def _hotel(tenant_id: uuid.UUID, *, enabled: bool = True) -> None:
    async with platform_session_scope() as session:
        await store_tenant_config(
            session,
            tenant_id,
            TenantConfig(
                profile=HotelProfile(city="Almaty", country_code="KZ"),
                timezone="Asia/Almaty",
                default_language="ru",
                memos_enabled=enabled,
            ),
        )


async def _member(
    client: AsyncClient, hotel: PortalHotel, name: str, role: StaffRole = StaffRole.STAFF
) -> uuid.UUID:
    """Сотрудник отеля вошёл с «Запомнить меня» и нарисовал образец подписи."""
    staff_login = unique_login()
    user_id = await create_staff_user(
        staff_login, tenant_id=hotel.tenant_id, role=role, display_name=name
    )
    await _login(client, staff_login, PASSWORD)
    sample = await client.post(
        f"{API}/signature/sample", json={"strokes": STROKES}, headers=SAME_ORIGIN
    )
    assert sample.status_code == 200, sample.text
    return user_id


async def _login(client: AsyncClient, staff_login: str, password: str) -> None:
    response = await client.post(
        f"{HOME}/login", data={"login": staff_login, "password": password, "remember": "1"}
    )
    assert response.status_code == 303


async def _phone(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    endpoint = f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"
    with tenant_context(tenant_id):
        await staff_push.save_subscription(
            user_id, endpoint, b64url_encode(public), b64url_encode(os.urandom(16))
        )
    return endpoint


def _pushes(fake: RecordingPush, endpoint: str) -> list[dict[str, str]]:
    return [json.loads(payload) for sent_to, payload in fake.sent if sent_to == endpoint]


async def _draft(client: AsyncClient, boss: uuid.UUID, signers: list[uuid.UUID]) -> str:
    created = await client.post(
        f"{API}/drafts",
        json={
            "title": TITLE,
            "body": "Прошу закупить 40 полотенец.\nК пятнице.",
            "addressee_id": str(boss),
            "signer_ids": [str(user_id) for user_id in signers],
        },
        headers=SAME_ORIGIN,
    )
    assert created.status_code == 200, created.text
    memo_id: str = created.json()["id"]
    return memo_id


async def _confirmed(
    client: AsyncClient, memo_id: str, intent: dict[str, str], *, password: str = PASSWORD
) -> str:
    """Окно подтверждения паролем кабинета: begin → complete; новый статус записи."""
    begun = await client.post(
        f"{API}/{memo_id}/begin", json={"intent": intent}, headers=SAME_ORIGIN
    )
    assert begun.status_code == 200, begun.text
    done = await client.post(
        f"{API}/{memo_id}/complete",
        json={"intent": intent, "challenge_id": begun.json()["challenge_id"], "password": password},
        headers=SAME_ORIGIN,
    )
    assert done.status_code == 200, done.text
    status: str = done.json()["status"]
    return status


async def test_memo_flow_over_http(
    stand: dict[str, AsyncClient], portal_hotel: PortalHotel, fake_push: RecordingPush
) -> None:
    await _hotel(portal_hotel.tenant_id)
    author, signer, boss = stand["author"], stand["signer"], stand["boss"]
    author_id = await _member(author, portal_hotel, "Айгуль Автор")
    signer_id = await _member(signer, portal_hotel, "Дана Подписант")
    boss_id = await _member(boss, portal_hotel, "Ерлан Старший", StaffRole.SENIOR)
    phones = {
        who: await _phone(portal_hotel.tenant_id, user_id)
        for who, user_id in (("author", author_id), ("signer", signer_id), ("boss", boss_id))
    }

    form = await author.get(f"{MEMOS}/new")
    assert form.status_code == 200
    assert "Ерлан Старший" in form.text and "Дана Подписант" in form.text
    memo_id = await _draft(author, boss_id, [signer_id])
    draft = await author.get(f"{MEMOS}/{memo_id}")
    assert draft.status_code == 200
    assert TITLE in draft.text and "Черновик" in draft.text
    assert (await signer.get(f"{MEMOS}/{memo_id}")).status_code == 404  # черновик — только автору

    assert await _confirmed(author, memo_id, {"kind": "send"}) == "signing"
    [push] = _pushes(fake_push, phones["signer"])
    assert push["title"] == texts.PUSH_TITLE and push["body"] == texts.PUSH_SIGN
    assert push["url"].endswith(f"/memos/{memo_id}") and TITLE not in json.dumps(push)

    waiting = await signer.get(MEMOS)
    assert waiting.status_code == 200 and TITLE in waiting.text
    assert 'aria-label="ждут вас: 1"' in waiting.text
    card = await signer.get(f"{MEMOS}/{memo_id}")
    assert "Айгуль Автор просит подписать записку." in card.text
    assert await _confirmed(signer, memo_id, {"kind": "sign"}) == "signing"
    assert TITLE not in (await signer.get(MEMOS)).text  # ответ дан — «Ждут меня» пуста

    submitted = await author.post(f"{API}/{memo_id}/submit", json={}, headers=SAME_ORIGIN)
    assert submitted.status_code == 200, submitted.text
    [push] = _pushes(fake_push, phones["boss"])
    assert push["body"] == texts.PUSH_RECEIVED

    returned = {"kind": "decide", "decision": "returned", "comment": "Уточните сумму"}
    assert await _confirmed(boss, memo_id, returned) == "returned"
    [push] = _pushes(fake_push, phones["author"])
    assert push["body"] == texts.PUSH_DECIDED and "Уточните" not in json.dumps(push)

    sheet = (await author.get(f"{MEMOS}/{memo_id}")).text
    for expected in ("Подписал(а)", "Отправил(а)", "Решение: На доработку", "Уточните сумму"):
        assert expected in sheet
    assert "подтверждено паролем кабинета" in sheet
    assert "Просмотры (2)" in sheet and "Дана Подписант —" in sheet
    assert "Создать новую на основе этой" in sheet

    again = await author.post(f"{API}/{memo_id}/based-on", json={}, headers=SAME_ORIGIN)
    assert again.status_code == 200, again.text
    assert again.json()["href"].endswith("/edit")
    edit = await author.get(again.json()["href"])
    assert edit.status_code == 200 and TITLE in edit.text
    new_card = await author.get(f"{MEMOS}/{again.json()['id']}")
    assert "Взамен" in new_card.text and "СЗ-" in new_card.text


async def test_stranger_and_section_off(
    stand: dict[str, AsyncClient], portal_hotel: PortalHotel
) -> None:
    await _hotel(portal_hotel.tenant_id)
    author, stranger = stand["author"], stand["stranger"]
    await _member(author, portal_hotel, "Айгуль Автор")
    await _member(stranger, portal_hotel, "Посторонний")
    memo_id = await _draft(author, portal_hotel.user_id, [])
    assert await _confirmed(author, memo_id, {"kind": "send"}) == "submitted"

    assert (await stranger.get(f"{MEMOS}/{memo_id}")).status_code == 404
    begun = await stranger.post(
        f"{API}/{memo_id}/begin", json={"intent": {"kind": "sign"}}, headers=SAME_ORIGIN
    )
    assert begun.status_code == 404
    assert begun.json()["error"]["code"] == memos_api.ERR_MEMO_NOT_FOUND
    assert TITLE not in (await stranger.get(f"{MEMOS}/all")).text

    await _hotel(portal_hotel.tenant_id, enabled=False)
    assert (await author.get(f"{MEMOS}/{memo_id}")).status_code == 404
    assert (await author.get(f"{MEMOS}/new")).status_code == 404
    assert (
        await author.post(f"{API}/{memo_id}/withdraw", json={}, headers=SAME_ORIGIN)
    ).status_code == 404


def _row(**fields: object) -> memos_api.SignatureRead:
    base: dict[str, object] = {
        "user_id": uuid.uuid4(),
        "role": memos_api.SignatureRole.SIGNER,
        "response": memos_api.SignatureResponse.SIGNED,
        "remark": None,
        "method": ConfirmationMethod.PASSKEY,
        "signed_at": datetime(2026, 11, 11, 9, 5, tzinfo=UTC),
        "sample": None,
        "proof_status": ProofStatus.VERIFIED,
    }
    return memos_api.SignatureRead.model_validate({**base, **fields})


def test_sheet_method_lines() -> None:
    """Строка способа подтверждения (§9, §4) — по поясу отеля, с особыми случаями."""
    zone = ZoneInfo("Asia/Almaty")
    reset = datetime(2026, 11, 10, 6, 0, tzinfo=UTC)
    when = "11.11.2026 14:05"
    assert memos_card.method_line(_row(), zone) == f"подтверждено с личного устройства, {when}"
    by_password = _row(
        method=ConfirmationMethod.PASSWORD,
        proof_status=ProofStatus.PASSWORD_ONLY,
        password_reset_at=reset,
    )
    assert memos_card.method_line(by_password, zone) == (
        f"подтверждено паролем кабинета, {when}; пароль перед этим сбрасывали 10.11.2026"
    )
    disowned = _row(proof_status=ProofStatus.DISOWNED)
    assert "владелец потом назвал чужим" in memos_card.method_line(disowned, zone)
    revoked = _row(key_reset_revoked_at=datetime(2026, 11, 12, 6, 0, tzinfo=UTC))
    assert memos_card.method_line(revoked, zone) == (
        f"подтверждено с устройства, которое потом отвязал сброс пароля 12.11.2026, {when}"
    )
    after_reset = _row(key_bound_after_reset_at=reset)
    assert memos_card.method_line(after_reset, zone).endswith(
        "; устройство привязано после сброса пароля 10.11.2026"
    )
    mismatch = _row(proof_status=ProofStatus.MISMATCH, key_reset_revoked_at=reset)
    assert memos_card.method_line(mismatch, zone) == (
        f"подтверждение не сходится с нынешним текстом записки, {when}"
    )
    assert memos_card.method_line(_row(signed_at=None, method=None), zone) == ""


async def test_key_after_reset_and_revoked_by_reset_named_in_sheet(
    stand: dict[str, AsyncClient], portal_hotel: PortalHotel
) -> None:
    """Подписант сменил пароль по ссылке, привязал телефон и подписал им; потом новый
    пароль по ссылке погасил ключ. Лист не пишет «с личного устройства» молча."""
    await _hotel(portal_hotel.tenant_id)
    author, signer = stand["author"], stand["signer"]
    await _member(author, portal_hotel, "Айгуль Автор")
    signer_login = unique_login()
    signer_id = await create_staff_user(
        signer_login,
        tenant_id=portal_hotel.tenant_id,
        role=StaffRole.STAFF,
        display_name="Дана Подписант",
    )
    reset = await issue_password_reset(
        signer_id, portal_hotel.tenant_id, actor_user_id=portal_hotel.user_id
    )
    await accept_password_reset(reset.reset_token, password=NEW_PASSWORD)
    await _login(signer, signer_login, NEW_PASSWORD)
    device = SoftAuthenticator()
    begun = await signer.post(
        f"{API}/signature/bind/begin", json={"password": NEW_PASSWORD}, headers=PHONE
    )
    assert begun.status_code == 200, begun.text
    bound = await signer.post(
        f"{API}/signature/bind/finish",
        json={"credential": device.register(begun.json()["options"])},
        headers=PHONE,
    )
    assert bound.status_code == 200, bound.text
    sample = await signer.post(
        f"{API}/signature/sample", json={"strokes": STROKES}, headers=SAME_ORIGIN
    )
    assert sample.status_code == 200

    memo_id = await _draft(author, portal_hotel.user_id, [signer_id])
    assert await _confirmed(author, memo_id, {"kind": "send"}) == "signing"
    opened = await signer.post(
        f"{API}/{memo_id}/begin", json={"intent": {"kind": "sign"}}, headers=SAME_ORIGIN
    )
    options = opened.json()["options"]
    assert options is not None  # ключ есть — окно предлагает устройство
    signed = await signer.post(
        f"{API}/{memo_id}/complete",
        json={
            "intent": {"kind": "sign"},
            "challenge_id": opened.json()["challenge_id"],
            "credential": device.sign(options),
        },
        headers=SAME_ORIGIN,
    )
    assert signed.status_code == 200, signed.text

    sheet = (await author.get(f"{MEMOS}/{memo_id}")).text
    assert "подтверждено с личного устройства" in sheet
    assert "устройство привязано после сброса пароля" in sheet

    again = await issue_password_reset(
        signer_id, portal_hotel.tenant_id, actor_user_id=portal_hotel.user_id
    )
    await accept_password_reset(again.reset_token, password=NEW_PASSWORD + "-2")
    sheet = (await author.get(f"{MEMOS}/{memo_id}")).text
    assert "подтверждено с устройства, которое потом отвязал сброс пароля" in sheet
    assert "подтверждено с личного устройства" not in sheet
