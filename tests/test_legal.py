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
from hospitality.platform.legal import (
    PRIVACY_POLICY_PATH,
    load_privacy_policy_markdown,
    privacy_policy_url,
)

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
    assert "Я соглашаюсь на обработку" in text
    assert "Мен дербес деректерімді" in text
    assert "I consent to the processing" in text


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
    assert "проверка юристом" not in published


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
