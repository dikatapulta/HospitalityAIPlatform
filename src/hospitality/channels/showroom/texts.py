"""Надписи 3D-витрины на трёх языках (spec 0069 §5): страница и экран кода.

Русские — spec 0069 §5 как есть (ответ диспетчера за основателя 03.11,
docs/founder-review/2026-11-03-pr801-showroom-texts.md); английские и казахские —
перевод окна PR (а), показан диспетчеру файлом до кода (запись там же). Порядок
языков — en первым: агенты из-за рубежа (§2.2). Тексты помощника и форм добавляют
PR (б) и (в) своими ключами.
"""

from __future__ import annotations

from typing import Final, Literal

Language = Literal["en", "ru", "kk"]
LANGUAGES: Final[tuple[Language, ...]] = ("en", "ru", "kk")
LANGUAGE_LABELS: Final[dict[Language, str]] = {"en": "EN", "ru": "RU", "kk": "ҚАЗ"}

TEXTS: Final[dict[Language, dict[str, str]]] = {
    "en": {
        "title": "{hotel} — 3D hotel showroom",
        "audience_group": "For groups",
        "audience_booking": "For guests",
        "disclaimer": (
            "This is a diagram of the building, not a photo: floors, sides and views come from "
            "the hotel's data. Room availability and prices are confirmed by the sales or "
            "reservations team."
        ),
        "view_mountains": "Mountains",
        "view_city": "City",
        "view_top": "From above",
        "view_street": "From the street",
        "reset": "Reset view",
        "floors": "Floors",
        "floor": "Floor {n}",
        "room_search": "Room, e.g. 1507",
        "find": "Find",
        "room_not_found": "There is no such room on the diagram",
        "panel_title": "Find rooms",
        "floor_from": "Floor from",
        "floor_to": "to",
        "side": "Side",
        "room_type": "Room type",
        "view": "View",
        "count": "How many rooms",
        "any": "Any",
        "any_side": "Any",
        "show": "Show",
        "matches": "Matching rooms: {n}",
        "no_matches": "No rooms match these conditions",
        "room": "Room {number} · floor {floor}",
        "price_note": "{price} — a guide price, confirmed by the reservations team",
        "no_price": "Price and availability will be confirmed by the reservations team",
        "no_webgl": "This browser can't show 3D — please open the page on another device.",
        "code_title": "Enter the access code",
        "code_open": "Open",
        "code_wrong": "The code didn't work",
        "code_limited": "Too many attempts — please try again later.",
    },
    "ru": {
        "title": "3D-витрина отеля {hotel}",
        "audience_group": "Для групп",
        "audience_booking": "Для гостей",
        "disclaimer": (
            "Это схема здания, а не фотография: этажи, стороны и виды — по данным отеля. "
            "Наличие номеров и цены уточняет отдел продаж или бронирования."
        ),
        "view_mountains": "Горы",
        "view_city": "Город",
        "view_top": "Сверху",
        "view_street": "С улицы",
        "reset": "Сначала",
        "floors": "Этажи",
        "floor": "{n} этаж",
        "room_search": "Номер, например 1507",
        "find": "Найти",
        "room_not_found": "Такого номера на схеме нет",
        "panel_title": "Подбор номеров",
        "floor_from": "Этаж от",
        "floor_to": "до",
        "side": "Сторона",
        "room_type": "Тип номера",
        "view": "Вид",
        "count": "Сколько номеров",
        "any": "Любой",
        "any_side": "Любая",
        "show": "Показать",
        "matches": "Подходит номеров: {n}",
        "no_matches": "Под эти условия номеров нет",
        "room": "Номер {number} · {floor} этаж",
        "price_note": "{price} — ориентир, подтверждает отдел бронирования",
        "no_price": "Цену и наличие уточнит отдел бронирования",
        "no_webgl": "Браузер не показывает 3D — откройте страницу на другом устройстве.",
        "code_title": "Введите код доступа",
        "code_open": "Открыть",
        "code_wrong": "Код не подошёл",
        "code_limited": "Слишком много попыток — попробуйте позже.",
    },
    "kk": {
        "title": "{hotel} қонақүйінің 3D-витринасы",
        "audience_group": "Топтарға",
        "audience_booking": "Қонақтарға",
        "disclaimer": (
            "Бұл фотосурет емес, ғимараттың сызбасы: қабаттар, жақтар мен көріністер — "
            "қонақүй деректері бойынша. Нөмірлердің бар-жоғын және бағасын сату немесе "
            "брондау бөлімі нақтылайды."
        ),
        "view_mountains": "Таулар",
        "view_city": "Қала",
        "view_top": "Жоғарыдан",
        "view_street": "Көшеден",
        "reset": "Басынан",
        "floors": "Қабаттар",
        "floor": "{n}-қабат",
        "room_search": "Нөмір, мысалы 1507",
        "find": "Табу",
        "room_not_found": "Сызбада мұндай нөмір жоқ",
        "panel_title": "Нөмір таңдау",
        "floor_from": "Қабат: бастап",
        "floor_to": "дейін",
        "side": "Жағы",
        "room_type": "Нөмір түрі",
        "view": "Көрініс",
        "count": "Қанша нөмір",
        "any": "Кез келген",
        "any_side": "Кез келген",
        "show": "Көрсету",
        "matches": "Сәйкес нөмірлер: {n}",
        "no_matches": "Бұл шарттарға сай нөмір жоқ",
        "room": "{number} нөмір · {floor}-қабат",
        "price_note": "{price} — бағдар баға, брондау бөлімі растайды",
        "no_price": "Бағасы мен бар-жоғын брондау бөлімі нақтылайды",
        "no_webgl": "Бұл браузер 3D көрсетпейді — бетті басқа құрылғыда ашыңыз.",
        "code_title": "Кіру кодын енгізіңіз",
        "code_open": "Ашу",
        "code_wrong": "Код сәйкес келмеді",
        "code_limited": "Тым көп әрекет — кейінірек қайталаңыз.",
    },
}


_BY_CODE: Final[dict[str, Language]] = {language: language for language in LANGUAGES}


def pick_language(requested: str | None, accept_language: str | None) -> Language:
    """Язык страницы: `?lang=`, затем первый знакомый из Accept-Language, иначе en."""
    if requested in _BY_CODE:
        return _BY_CODE[requested]
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in _BY_CODE:
            return _BY_CODE[code]
    return "en"
