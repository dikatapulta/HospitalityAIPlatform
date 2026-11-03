"""Стоковые фото демо-отеля (#794): товары room service из сида и «Забытые вещи».

Сид: фото получает товар без фото и только на staging демо-отеля; товар с фото
менеджера не трогается; повтор ничего не меняет; сбой чтения файла не роняет сид.
Файлы: у каждого товара сида и каждой находки с фото — свой файл, 640 px по
длинной стороне, до 60 КБ, всего до 2 МБ, и строка в `SOURCES.md` с лицензией
Unsplash или Pexels.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from pathlib import Path

import pytest
from PIL import Image
from structlog.testing import capture_logs

from hospitality.modules.room_service.api import (
    ItemRead,
    RoomServiceCatalog,
    get_catalog,
    set_item_photo,
)
from hospitality.platform.seed import seed_demo_tenant
from hospitality.shared.ports.file_storage import FileStoragePort
from hospitality.shared.tenancy import tenant_context
from hospitality.staff_portal import demo_lost_found
from hospitality.staff_portal.rendering import STATIC_ASSETS
from hospitality.tools import demo_room_service
from hospitality.tools.demo_room_service import (
    DEMO_CATALOGS,
    DEMO_ITEM_PHOTOS,
    DEMO_ITEMS_WITHOUT_PHOTO,
    DEMO_PHOTOS_DIR,
)
from hospitality.tools.seed import seed_demo_data
from tests.test_file_content import make_png

LOST_FOUND_STATIC = Path(demo_lost_found.__file__).with_name("static")
LOST_FOUND_SOURCES = LOST_FOUND_STATIC / "demo_lost_found_SOURCES.md"
MAX_SIDE = 640
MAX_FILE_BYTES = 60 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
LICENSES = frozenset({"Pexels License", "Unsplash License"})


@pytest.fixture
def on_staging(monkeypatch: pytest.MonkeyPatch, fake_file_storage: FileStoragePort) -> None:
    """Защиты демо-инструментов пройдены — как на staging демо-отеля."""
    monkeypatch.setattr(demo_room_service, "staging_refusals", lambda *, confirmed: [])


async def _items(tenant_id: uuid.UUID) -> dict[str, ItemRead]:
    with tenant_context(tenant_id):
        return {
            item.names.en: item
            for catalog in RoomServiceCatalog
            for section in (await get_catalog(catalog, include_hidden=True)).sections
            for item in section.items
        }


@pytest.mark.usefixtures("canonical_database")
async def test_seed_attaches_photos_on_staging_once(on_staging: None) -> None:
    """Вне staging фото нет; на staging — каждому товару сида со своим файлом,
    кроме того, где менеджер уже поставил своё; повторный деплой не меняет ни
    одного фото."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(demo_room_service, "staging_refusals", lambda *, confirmed: ["не staging"])
        await seed_demo_data()
    tenant_id = await seed_demo_tenant()
    assert all(item.photo_file_id is None for item in (await _items(tenant_id)).values())
    with tenant_context(tenant_id):
        own = await set_item_photo(
            (await _items(tenant_id))["Cappuccino"].id,
            make_png(),
            "own.png",
            uploaded_by_user_id=None,
        )

    await seed_demo_data()
    first = {name: item.photo_file_id for name, item in (await _items(tenant_id)).items()}
    await seed_demo_data()
    second = {name: item.photo_file_id for name, item in (await _items(tenant_id)).items()}

    assert {name for name, photo in first.items() if photo is None} == DEMO_ITEMS_WITHOUT_PHOTO
    assert first["Cappuccino"] == own.photo_file_id
    assert second == first


@pytest.mark.usefixtures("canonical_database")
async def test_photo_read_failure_keeps_seed_going(
    on_staging: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Файла нет (битый образ, сбой диска) — WARNING на товар, сид доходит до конца."""
    monkeypatch.setattr(demo_room_service, "DEMO_PHOTOS_DIR", tmp_path)

    with capture_logs() as logs:
        await seed_demo_data()

    tenant_id = await seed_demo_tenant()
    items = await _items(tenant_id)
    failed = [log for log in logs if log["event"] == "demo_room_service_photo_failed"]
    assert len(items) == len(DEMO_ITEM_PHOTOS) + len(DEMO_ITEMS_WITHOUT_PHOTO)
    assert all(item.photo_file_id is None for item in items.values())
    assert len(failed) == len(DEMO_ITEM_PHOTOS)
    assert {log["log_level"] for log in failed} == {"warning"}


def _sources_rows(sources: Path) -> dict[str, str]:
    """Файл → лицензия из таблицы `SOURCES.md` (первый и последний столбцы)."""
    rows: dict[str, str] = {}
    for line in sources.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        match = re.fullmatch(r"`([\w.-]+\.webp)`", cells[0])
        if match:
            rows[match.group(1)] = cells[-1]
    return rows


def _check_photos(directory: Path, pattern: str, names: set[str], sources: Path) -> int:
    """Файлы по образцу — ровно `names`, у каждого лицензия и размеры; итог — байты."""
    assert {path.name for path in directory.glob(pattern)} == names
    licenses = _sources_rows(sources)
    assert set(licenses) == names
    assert set(licenses.values()) <= LICENSES
    total = 0
    for name in names:
        path = directory / name
        with Image.open(path) as image:
            assert max(image.size) == MAX_SIDE, name
        size = path.stat().st_size
        assert size <= MAX_FILE_BYTES, name
        total += size
    return total


def test_every_photo_is_small_and_sourced() -> None:
    """Фото у каждого товара сида, кроме названных без фото, и у каждой находки,
    кроме документа и карты; размеры — по issue; лишних файлов нет; у каждого —
    источник и лицензия."""
    seeded = {
        item[0]
        for sections in DEMO_CATALOGS.values()
        for section in sections
        for item in section[3]
    }
    assert set(DEMO_ITEM_PHOTOS) == seeded - DEMO_ITEMS_WITHOUT_PHOTO
    assert seeded >= DEMO_ITEMS_WITHOUT_PHOTO
    finds = demo_lost_found.build_demo_lost_found_context(date(2026, 11, 5), None)["finds"]
    found_names = {
        find["photo_url"].split("/")[-1].split("?")[0] for find in finds if not find["no_photo"]
    }
    assert all(find["photo_url"] == "" for find in finds if find["no_photo"])
    assert found_names <= set(STATIC_ASSETS)

    total = _check_photos(
        DEMO_PHOTOS_DIR, "*.webp", set(DEMO_ITEM_PHOTOS.values()), DEMO_PHOTOS_DIR / "SOURCES.md"
    )
    total += _check_photos(
        LOST_FOUND_STATIC, "demo_lost_found_*.webp", found_names, LOST_FOUND_SOURCES
    )
    assert total <= MAX_TOTAL_BYTES
