"""Блок «City guide» системного промпта гостя (spec 0085 §4, #952).

Справочник города — данные модуля `concierge`; здесь он становится текстом для
модели. Блок, а не инструмент — по канону справочника отеля (spec 0036 §4):
инструмент потребовал бы второго обращения к модели на каждый вопрос о городе,
а оркестратор ходит к модели за ход один раз.

Блок стабилен у отеля неделями, поэтому стоит в кэшируемом префиксе — сразу
после справочника отеля, до блоков хода (#138). Флаг выключен или города нет —
пустая строка: промпт прежний байт в байт. Текст блока пересказан в spec 0085 §4:
правишь здесь — правишь там тем же PR.
"""

from __future__ import annotations

from hospitality.modules.concierge import api as concierge_api
from hospitality.platform.config import TenantConfig
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)


def city_guide_block(config: TenantConfig | None) -> str:
    """Блок для отеля с включённым справочником; иначе — пустая строка.

    `config` — то же чтение конфига, что у хода оркестратора (spec 0036 §4).
    """
    if config is None or not config.city_guide_enabled:
        return ""
    guide = concierge_api.city_guide_for(config.profile.city, config.profile.country_code)
    if guide is None:
        # Флаг включён, а справочника для города отеля нет: гость не получит ничего
        # лишнего, но включивший должен это увидеть — отсюда warning, а не info.
        logger.warning(
            "city_guide_not_found",
            city=config.profile.city,
            country_code=config.profile.country_code,
        )
        return ""
    block = render_city_guide_block(guide)
    # Прибор цены, парный `hotel_facts_in_context`: блок едет в каждый ход (§4).
    logger.info(
        "city_guide_in_context", city=guide.key, entries=len(guide.entries), chars=len(block)
    )
    return block


def render_city_guide_block(guide: concierge_api.CityGuide) -> str:
    """Текст блока: записи в порядке файла, местное имя в скобках, ссылка карты в конце."""
    lines = [
        "",
        "",
        f"# City guide: {guide.name}",
        "",
        "General information about the city, compiled and checked by the platform —",
        "not written by this hotel and not a service of this hotel.",
        "",
    ]
    for entry in guide.entries:
        local_name = f" ({entry.local_name})" if entry.local_name else ""
        map_url = guide.map_url(entry)
        link = f" Map: {map_url}" if map_url else ""
        lines.append(f"- {entry.topic}{local_name}: {entry.text}{link}")
    lines += [
        "",
        "How to use this block:",
        "- Answer a question about the city only from these lines, briefly. Never add",
        "  places, prices, opening hours, distances or routes that are not written here,",
        "  even if you believe you know them.",
        "- Present it as general city information, never as the hotel's own service or",
        "  rule. Opening hours, prices and conditions change: suggest checking them at",
        "  the reception desk before going.",
        "- Anything about the hotel itself is decided by the hotel facts and the rules",
        "  above, never by this block.",
        "- When the guest asks where a place is or how to get there, give its Map link",
        "  exactly as written. Never build, shorten or change a link yourself.",
        "- Keep a local name in brackets as written: the guest can show it to a taxi",
        "  driver.",
        "- If no line covers a question about the city, do not guess: say you do not",
        "  have this information, point the guest to the reception desk, and call",
        "  `report_unanswered_question` — the same as for a question about the hotel.",
    ]
    return "\n".join(lines)
