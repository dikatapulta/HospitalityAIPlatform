"""Запись о согласии гостя — доказательство на три года, без имени (spec 0032 §6, #948).

Согласие гость даёт в двух местах, и оба пишут сюда тем же кодом, что ставит
`consent_at`:

- привязка к Stay (код заселения или ссылка с листка, веб и Telegram) —
  `stay_consent_record` в транзакции рождения сессии: комната и даты Stay;
- consent-gate Telegram до ввода кода — `record_consent_evidence`: Stay ещё нет.

Строки только вставляются; срок — `consent_record_retention_days`, удаляет их
`stay_retention.delete_expired_stay_data`.
"""

from __future__ import annotations

from datetime import datetime

from hospitality.modules.guests.models import ConsentRecord, Stay
from hospitality.shared.db import session_scope


def stay_consent_record(
    stay: Stay, *, channel: str, consent_version: str, consent_at: datetime
) -> ConsentRecord:
    """Строка о согласии, данном при привязке к Stay; в сессию её кладёт вызывающий."""
    return ConsentRecord(
        channel=channel,
        consent_at=consent_at,
        consent_version=consent_version,
        room_number=stay.room_number,
        check_in_at=stay.check_in_at,
        check_out_at=stay.check_out_at,
    )


async def record_consent_evidence(
    *, channel: str, consent_version: str, consent_at: datetime
) -> None:
    """Согласие вне привязки к Stay (consent-gate Telegram) — своя транзакция.

    Канал зовёт её ДО отметки согласия на диалоге: упади запись, согласия нет и
    гость нажмёт кнопку ещё раз; обратный порядок дал бы согласие, которое нечем
    подтвердить, когда диалог уйдёт с ретеншном. Повтор нажатия — вторая строка:
    журналу доказательств дубль безвреден.
    """
    async with session_scope() as session:
        session.add(
            ConsentRecord(channel=channel, consent_at=consent_at, consent_version=consent_version)
        )
