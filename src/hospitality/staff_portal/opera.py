"""Выгрузка Opera на странице заселения (spec 0058 §2.5): полоса, расхождения, карточки.

Только у тенанта с `pms_source: opera_report` (`HotelTiming.pms_interval_minutes`
не None); у отеля с ручным заселением страница прежняя. Данные — одним вызовом
`guests_api.get_pms_status` (P-5), здесь — только тексты. Тексты показаны
основателю до кода (spec 0058 §2.5); новое здесь — дата в полосе «не
обновлялись», когда снимок не сегодняшний, и склонение числа проживаний.
"""

from __future__ import annotations

from datetime import datetime, tzinfo

from hospitality.modules.guests import api as guests_api

Kind = guests_api.PmsDiscrepancyKind


async def read_status(interval_minutes: int | None) -> guests_api.PmsStatus | None:
    """Статус выгрузки тенанта запроса; None — отель заселяет вручную."""
    if interval_minutes is None:
        return None
    return await guests_api.get_pms_status(interval_minutes=interval_minutes)


def _stays_word(count: int) -> str:
    """«проживание / проживания / проживаний» по числу."""
    if count % 10 == 1 and count % 100 != 11:
        return "проживание"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "проживания"
    return "проживаний"


def _since(moment: datetime, zone: tzinfo, now: datetime) -> str:
    """«14:05» сегодня по отелю, иначе «01.11 14:05»."""
    local = moment.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return f"{local:%H:%M}"
    return f"{local:%d.%m %H:%M}"


def strip_text(status: guests_api.PmsStatus, zone: tzinfo, now: datetime) -> str | None:
    """Полоса над формой заселения — одна из трёх, важная первой; None — всё в порядке.

    Устаревший снимок важнее придержанных выездов: новые заселения не приходят
    вовсе, и ресепшену сначала нужно знать именно это.
    """
    if status.taken_at is None:
        return "Данные из Opera ещё не приходили. Заселяйте гостей здесь, как раньше."
    if status.stale:
        return (
            f"Данные из Opera не обновлялись с {_since(status.taken_at, zone, now)}. "
            "Новые заселения сюда сами не попадут — заселяйте гостей здесь."
        )
    if status.held_closures:
        count = status.held_closures
        return (
            f"Из последней выгрузки Opera пропало сразу {count} {_stays_word(count)} — "
            "похоже, файл неполный. Выезды по ней не оформлены: проверьте в Opera "
            "и оформите выезд здесь, если гость уехал."
        )
    return None


def _discrepancy_text(item: guests_api.PmsDiscrepancy) -> str:
    if item.kind is Kind.OCCUPIED_BY_MANUAL and item.arrival and item.manual_check_in:
        return (
            f"Комната {item.room_number}: в Opera заселена бронь {item.booking_ref} "
            f"с {item.arrival:%d.%m}, а здесь — гость, заселённый вручную "
            f"{item.manual_check_in:%d.%m}. Если он уехал — оформите выезд, "
            "бронь заселится сама."
        )
    if item.kind is Kind.MOVE_BLOCKED and item.target_room:
        return (
            f"Бронь {item.booking_ref} в Opera переехала в {item.target_room}, "
            f"но {item.target_room} здесь занята."
        )
    other = f" (бронь {item.other_booking_ref})" if item.other_booking_ref else ""
    return (
        f"Комната {item.room_number}: в Opera заселена бронь {item.booking_ref}, "
        f"а здесь — другое проживание{other}."
    )


def discrepancy_rows(status: guests_api.PmsStatus) -> list[dict[str, str]]:
    """Строки блока «Не сходится с Opera»; `room_number` — чью карточку открыть."""
    return [
        {"text": _discrepancy_text(item), "room_number": item.room_number}
        for item in status.discrepancies
    ]


def card_note(stay: guests_api.StayRead, status: guests_api.PmsStatus | None) -> str | None:
    """Строка карточки Stay об Opera; None — нечего сказать."""
    if status is None:
        return None
    if stay.source is guests_api.StaySource.PMS:
        return (
            "Из Opera. Комнату и даты меняйте в Opera — правка здесь продержится "
            f"до следующей выгрузки, около {status.interval_minutes} минут."
        )
    if guests_api.missing_from_pms(stay, status):
        return (
            "Нет в выгрузке Opera. Проверьте номер комнаты; если гость уехал — "
            "оформите выезд здесь."
        )
    return None
