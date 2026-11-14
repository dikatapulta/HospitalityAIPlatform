"""Юридический слой: страница политики и дословность текстов согласия (spec 0029).

Два разных класса проверки в одном файле, потому что предмет один — то, что
видит гость перед первым диалогом:

- `GET /legal/privacy` отдаёт политику и НЕ отдаёт служебную преамбулу
  (`docs/legal/*` начинаются с блока «что показать юристу» — он не для гостя);
- тексты согласия в коде (`channels/common/consent.py`) дословно совпадают с
  `docs/legal/consent-text.md`. Копия в коде — сознательное решение (spec 0029
  §2), и единственное, что делает её безопасной, — этот тест: разъехавшийся
  текст согласия юридически хуже, чем его отсутствие.
"""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from hospitality.app import create_app
from hospitality.channels.common import consent
from hospitality.platform.config import (
    DataOwner,
    HotelProfile,
    TenantConfig,
    store_tenant_config,
)
from hospitality.platform.legal import (
    PRIVACY_POLICY_PATH,
    load_privacy_policy_markdown,
    privacy_policy_url,
)
from hospitality.platform.models import Tenant
from hospitality.shared.db import platform_session_scope

CONSENT_DOC = "docs/legal/consent-text.md"
# Заголовки языковых разделов consent-text.md → коды языков в коде.
_SECTIONS = {"Қазақша": "kk", "Русский": "ru", "English": "en"}


def _document_sections() -> dict[str, tuple[str, str]]:
    """Разобрать consent-text.md: язык → (полный текст, надпись кнопки).

    Полный текст лежит в blockquote (строки `> …`, абзацы разделены строкой
    `>`), надпись — в строке `**Кнопка:** \\`…\\``.
    """
    with open(CONSENT_DOC, encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    sections: dict[str, tuple[str, str]] = {}
    language: str | None = None
    paragraphs: list[list[str]] = []
    button: str | None = None

    def commit() -> None:
        if language is None:
            return
        text = "\n\n".join(" ".join(paragraph) for paragraph in paragraphs if paragraph)
        assert button is not None, f"в разделе {language} нет строки **Кнопка:**"
        sections[language] = (text, button)

    for line in lines:
        heading = line.removeprefix("## ").strip() if line.startswith("## ") else None
        if heading in _SECTIONS:
            commit()
            language, paragraphs, button = _SECTIONS[heading], [], None
            continue
        if language is None:
            continue
        if line.startswith(">"):
            body = line.removeprefix(">").strip()
            if not body:
                paragraphs.append([])
            elif body.startswith("**"):
                continue  # подзаголовок «**Полный текст:**» внутри цитаты не бывает
            else:
                if not paragraphs:
                    paragraphs.append([])
                paragraphs[-1].append(body)
            continue
        match = re.match(r"\*\*(?:Кнопка|Button):\*\*\s*`(.+)`", line)
        if match:
            button = match.group(1)
    commit()
    return sections


def test_consent_version_matches_legal_doc() -> None:
    """Версия в коде — та, что объявлена юрпакетом (иначе гейт врёт о версии)."""
    with open(CONSENT_DOC, encoding="utf-8") as handle:
        document = handle.read()
    assert f'`consent_version = "{consent.CONSENT_VERSION}"`' in document


def test_smoke_suite_knows_current_consent_version() -> None:
    """Smoke-набор — чёрный ящик и версию согласия хардкодит (tests/smoke/README).

    Значит, при подъёме версии его строка обязана подняться вместе с кодом:
    иначе гость smoke'а жмёт кнопку прежней версии, гейт его не пропускает, и
    приёмка основателя падает на ровном месте.
    """
    from tests.smoke import test_guest_scenarios as smoke

    assert smoke.CONSENT_VERSION == consent.CONSENT_VERSION


@pytest.mark.parametrize("language", ["kk", "ru", "en"])
def test_consent_text_matches_legal_doc(language: str) -> None:
    """Текст в коде дословно равен тексту документа — сокращать его нельзя."""
    expected_text, expected_button = _document_sections()[language]
    assert consent.raw_consent_text(language) == expected_text
    assert consent.raw_button_label(language) == expected_button


def test_consent_text_substitutes_policy_url() -> None:
    """Плейсхолдер ссылки заменяется реальным URL: согласие без политики неполно."""
    text = consent.consent_text("ru")
    assert privacy_policy_url() in text
    assert "[ссылка на политику]" not in text


def test_unknown_language_shows_all_three_versions() -> None:
    """Язык гостя неизвестен → показываются kk+ru+en (docs/legal/consent-text.md)."""
    text = consent.consent_text(consent.normalize_language("de"))
    assert "Продолжая, вы соглашаетесь" in text
    assert "Жалғастыра отырып" in text
    assert "By continuing, you agree" in text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("ru", "ru"), ("ru-RU", "ru"), ("EN-gb", "en"), ("kk", "kk"), ("de", None), (None, None)],
)
def test_normalize_language(raw: str | None, expected: str | None) -> None:
    assert consent.normalize_language(raw) == expected


def test_policy_markdown_drops_internal_preamble() -> None:
    """Служебный блок «до публикации: вставить реквизиты…» гостю не показывается."""
    published = load_privacy_policy_markdown()
    assert published.startswith("## Қазақша")
    assert "публикуемая страница" not in published


async def test_privacy_policy_page_is_public() -> None:
    """Страница политики доступна без токена: на неё ссылается текст согласия."""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(PRIVACY_POLICY_PATH)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    body = response.text
    assert "Қонақ чатының құпиялылық саясаты" in body
    assert "Guest Chat Privacy Policy" in body


# --- Страница отеля: собственник базы данных в §1 (spec 0029 §11, #948) ---

# Угловые скобки в наименовании — проверка экранирования: значение отеля уходит в HTML.
_OWNER = DataOwner(name="ТОО «Тест <Отель>»", bin_iin="123456789012")
_OWNER_NAME_HTML = "ТОО «Тест &lt;Отель&gt;»"
_OWNER_PARAGRAPH_RU = "Собственник —"


async def _create_hotel(
    slug: str, *, owner: DataOwner | None, configured: bool = True, broken: bool = False
) -> None:
    async with platform_session_scope() as session:
        tenant = Tenant(slug=slug, name="Demo Hotel")
        session.add(tenant)
        await session.flush()
        if broken:
            tenant.config = {"schema_version": 1}  # без обязательных полей: ERR-PLATFORM-006
        elif configured:
            await store_tenant_config(
                session,
                tenant.id,
                TenantConfig(
                    profile=HotelProfile(city="Almaty", country_code="KZ"),
                    timezone="Asia/Almaty",
                    default_language="ru",
                    data_owner=owner,
                ),
            )


async def _get(path: str) -> str:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 200
    return response.text


def test_owner_paragraph_is_one_per_language() -> None:
    """Места наименования и БИН — в одном абзаце на язык: иначе код опубликует полфразы."""
    published = load_privacy_policy_markdown()
    assert published.count("{hotel_legal_name}") == 3
    assert published.count("{hotel_bin}") == 3
    for paragraph in re.split(r"\n[ \t]*\n", published):
        assert ("{hotel_legal_name}" in paragraph) == ("{hotel_bin}" in paragraph)


async def test_generic_policy_page_has_no_owner_paragraph() -> None:
    """Общая страница отеля не знает: абзац о собственнике не публикуется вовсе."""
    body = await _get(PRIVACY_POLICY_PATH)
    assert "{hotel" not in body
    assert _OWNER_PARAGRAPH_RU not in body
    assert "Собственник базы данных — отель" in body  # общая фраза §1 на месте


async def test_hotel_policy_page_names_data_owner(canonical_database: None) -> None:
    """Страница отеля называет юрлицо и БИН на всех трёх языках (ст. 8 п. 4 пп. 1)."""
    await _create_hotel("owner-hotel", owner=_OWNER)
    body = await _get(f"{PRIVACY_POLICY_PATH}/owner-hotel")
    assert body.count(_OWNER_NAME_HTML) == 3
    assert body.count(_OWNER.bin_iin) == 3
    assert f"{_OWNER_PARAGRAPH_RU} {_OWNER_NAME_HTML}, БИН/ИИН {_OWNER.bin_iin}." in body
    assert "<Отель>" not in body
    assert "{hotel" not in body


@pytest.mark.parametrize("case", ["no_owner", "no_config", "broken_config", "unknown_slug"])
async def test_hotel_policy_page_without_owner_is_generic(
    canonical_database: None, case: str
) -> None:
    """Отель без настройки, без конфига, со сломанным конфигом и чужой адрес — общий
    текст, а не ошибка: на эту страницу ведёт согласие, она обязана открываться."""
    if case != "unknown_slug":
        await _create_hotel(
            "plain-hotel",
            owner=None,
            configured=case == "no_owner",
            broken=case == "broken_config",
        )
    body = await _get(f"{PRIVACY_POLICY_PATH}/plain-hotel")
    assert "Guest Chat Privacy Policy" in body
    assert "{hotel" not in body
    assert _OWNER_PARAGRAPH_RU not in body


def test_consent_text_links_hotel_policy_page() -> None:
    """Гостевой канал передаёт адрес отеля — ссылка согласия ведёт на его страницу."""
    url = privacy_policy_url(tenant_slug="demo-hotel")
    assert url.endswith(f"{PRIVACY_POLICY_PATH}/demo-hotel")
    assert f"{url}" in consent.consent_text("ru", tenant_slug="demo-hotel")
    assert privacy_policy_url().endswith(PRIVACY_POLICY_PATH)


# --- 3D-витрина (spec 0069 §2.6, PR (в) #805): своя политика и своя строка согласия ---

SHOWROOM_DOC = "docs/legal/showroom-privacy.md"


def _showroom_consent_lines() -> dict[str, str]:
    """Строки согласия из преамбулы политики витрины: `> - en: \\`…\\``."""
    with open(SHOWROOM_DOC, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    found: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^> - (en|ru|kk): `(.+)`$", line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


@pytest.mark.parametrize("language", ["en", "ru", "kk"])
def test_showroom_consent_matches_policy_doc(language: str) -> None:
    """Строка под формами витрины дословно равна документу (копия в коде — `consent.py`)."""
    from hospitality.channels.showroom import consent as showroom_consent

    assert showroom_consent.CONSENT_TEXTS[language] == _showroom_consent_lines()[language]


async def test_showroom_policy_page_is_public_without_preamble() -> None:
    """`/legal/showroom-privacy` — на неё ссылается строка согласия; преамбула скрыта."""
    from hospitality.platform.legal import SHOWROOM_PRIVACY_PATH

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(SHOWROOM_PRIVACY_PATH)
    assert response.status_code == 200
    body = response.text
    assert "3D Showroom Privacy Policy" in body
    assert "Политика конфиденциальности 3D-витрины отеля" in body
    assert "вариант окна" not in body
    assert "365" in body
