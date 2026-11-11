"""«Пульс пользы» — пользуются ли отелем кабинет и «Наставник» (spec 0035 §8.1, #1050 ч. 2).

Сторожа ловят ошибки, а то, что ошибок не оставляет, — нет: кабинет не открывают,
«Наставнику» не пишут, экзамен бросают. Здесь две вещи, обе основателю, обе без LLM:

- **строки копии утренней сводки** (`usage_pulse` + `pulse_lines`) — входы персонала,
  подключения гостей, ответы гостю не на его языке (§8.2, #1043), вопросы «Наставнику»,
  экзамены за сутки отеля; копию собирает
  `daily_summary.py`, сводка отеля этих строк не получает;
- **сигнал «тихий день»** (`send_quiet_day_alerts`, ERR-OPS-022) — у отеля назначен
  экзамен, а с его полуночи до полудня никто не вошёл в кабинет, не спросил «Наставника»
  и не начал экзамен. Один на сутки отеля: ключ `platform:usage_quiet:<день>` на исходящем
  `Message` пишется после успешной отправки (P-8, канон копии сводки).

Числа считают владельцы данных (P-5): входы персонала — `platform/staff_logins`, гостей —
`modules/guests`, язык ответа — `channels/common/reply_language`, «Наставник» —
`modules/mentor`. Счётчик процесса для суток не годится:
рестарт его обнуляет.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Final, NamedTuple

import structlog

from hospitality.channels.common.reply_language import ReplyLanguageCount, count_reply_language
from hospitality.channels.common.store import (
    ensure_conversation,
    notification_already_sent,
    record_outbound_message,
)
from hospitality.channels.telegram.normalize import CHANNEL
from hospitality.channels.telegram.routing import current_correlation_id
from hospitality.modules.guests import api as guests_api
from hospitality.modules.mentor import api as mentor_api
from hospitality.platform.config import (
    list_configured_tenant_ids,
    load_tenant_config,
    load_tenant_name,
)
from hospitality.platform.staff_logins import StaffLogins, count_staff_logins
from hospitality.shared.alerting import AlertSender, format_alert
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, utc_now
from hospitality.shared.errors import AppError
from hospitality.shared.humanize import plural
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import record_usage_quiet_alert
from hospitality.shared.tenancy import tenant_context

logger = get_logger(module=__name__)

# Код каталога (docs/runbooks/errors.md, R-8): и сам сигнал, и сбой его проверки —
# различает событие лога (`usage_quiet_alert_sent` / `usage_quiet_check_failed`).
ERR_USAGE_QUIET_DAY: Final = "ERR-OPS-022"
QUIET_IDEMPOTENCY_KEY_PREFIX: Final = "platform:usage_quiet"
# К полудню смена уже дошла до кабинета; раньше тишина — ещё не сигнал.
QUIET_CHECK_LOCAL_TIME: Final = time(12, 0)


class UsagePulse(NamedTuple):
    """Числа «пульса пользы» за окно; `mentor` — `None`, когда «Наставник» у отеля выключен."""

    staff: StaffLogins
    guests: int
    mentor: mentor_api.MentorActivity | None
    # Ответы гостю, сверенные по алфавиту (§8.2, #1043); 0 проверок — строки нет.
    reply_language: ReplyLanguageCount


async def usage_pulse(
    tenant_id: uuid.UUID,
    *,
    mentor_enabled: bool,
    created_after: datetime,
    created_before: datetime,
) -> UsagePulse:
    """Собрать числа у владельцев; зовётся внутри `tenant_context(tenant_id)`."""
    mentor = (
        await mentor_api.mentor_activity(created_after=created_after, created_before=created_before)
        if mentor_enabled
        else None
    )
    return UsagePulse(
        staff=await count_staff_logins(
            tenant_id, created_after=created_after, created_before=created_before
        ),
        guests=await guests_api.count_guest_logins(
            created_after=created_after, created_before=created_before
        ),
        mentor=mentor,
        reply_language=await count_reply_language(
            created_after=created_after, created_before=created_before
        ),
    )


def pulse_lines(pulse: UsagePulse) -> list[str]:
    """Строки копии основателю (тексты — spec 0035 §8.1).

    Строки «Наставника» — только у отеля, где он включён: до включения копия не говорит о
    разделе, которого нет. Не засчитанные ответы — только ненулевым числом. Язык ответа
    гостю (§8.2) — только когда было что сверить.
    """
    staff = pulse.staff
    logins = f"{staff.logins}"
    if staff.logins:
        logins += (
            f" ({staff.people} {plural(staff.people, 'сотрудник', 'сотрудника', 'сотрудников')})"
        )
    lines = [f"👥 Входы в кабинет: {logins}. Гости подключились к чату: {pulse.guests}."]
    language = pulse.reply_language
    if language.checked:
        mark = "⚠️" if language.mismatched else "🌐"
        lines.append(
            f"{mark} Ответы не на языке гостя (по алфавиту): "
            f"{language.mismatched} из {language.checked}."
        )
    mentor = pulse.mentor
    if mentor is None:
        return lines
    if mentor.turns:
        lines.append(
            f"🎓 «Наставник»: {mentor.turns} "
            f"{plural(mentor.turns, 'вопрос', 'вопроса', 'вопросов')} от {mentor.people} "
            f"{plural(mentor.people, 'сотрудника', 'сотрудников', 'сотрудников')}."
        )
    else:
        lines.append("🎓 «Наставник»: вопросов не было.")
    if mentor.exams_started or mentor.exams_finished:
        lines.append(
            f"📝 Экзамены: начато {mentor.exams_started}, закончено {mentor.exams_finished}, "
            f"сдано {mentor.exams_passed}."
        )
    else:
        lines.append("📝 Экзамены: не начинали.")
    if mentor.ungraded:
        lines.append(
            f"⚠️ Не засчитано ответов на экзаменах: {mentor.ungraded} — оценку не удалось разобрать."
        )
    return lines


async def send_quiet_day_alerts(*, alert_sender: AlertSender | None, alert_chat_id: str) -> int:
    """Один прогон сигнала «тихий день»; вернуть число отправленных алертов.

    Тракт алертов не настроен — шаг молчит: сигнал адресован только основателю. Сбой на
    одном отеле не отменяет остальных; ключ не записан — следующий прогон повторит.
    """
    if alert_sender is None or not alert_chat_id:
        return 0
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        async with platform_session_scope() as session:
            tenant_ids = await list_configured_tenant_ids(session)
        sent = 0
        for tenant_id in tenant_ids:
            try:
                sent += await _check_tenant(tenant_id, alert_sender, alert_chat_id)
            except Exception:
                logger.error(
                    "usage_quiet_check_failed",
                    error_code=ERR_USAGE_QUIET_DAY,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
                record_usage_quiet_alert("failed")
        return sent


async def _check_tenant(tenant_id: uuid.UUID, alert_sender: AlertSender, alert_chat_id: str) -> int:
    """Проверить один отель; 1 — алерт ушёл. Дешёвые отсечки — раньше счёта."""
    with structlog.contextvars.bound_contextvars(tenant_id=str(tenant_id)):
        try:
            async with platform_session_scope() as session:
                config = await load_tenant_config(session, tenant_id)
                tenant_name = await load_tenant_name(session, tenant_id)
        except AppError as error:
            logger.warning("usage_quiet_config_unavailable", error_code=error.code)
            return 0
        if not config.mentor_enabled:
            return 0
        local_now = utc_now().astimezone(config.tzinfo)
        if local_now.time() < QUIET_CHECK_LOCAL_TIME:
            return 0
        key = f"{QUIET_IDEMPOTENCY_KEY_PREFIX}:{local_now.date().isoformat()}"
        with tenant_context(tenant_id):
            if await notification_already_sent(key):
                return 0
            if not await mentor_api.has_active_exam_assignment():
                return 0
            day_start = datetime.combine(local_now.date(), time(0), tzinfo=config.tzinfo)
            now = utc_now()
            staff = await count_staff_logins(tenant_id, created_after=day_start, created_before=now)
            if staff.logins:
                return 0
            activity = await mentor_api.mentor_activity(created_after=day_start, created_before=now)
            if activity.turns or activity.exams_started:
                return 0
            text = _quiet_text(tenant_id, tenant_name, local_now)
            conversation_id = await ensure_conversation(CHANNEL, alert_chat_id)
            await alert_sender(text)
            await record_outbound_message(
                conversation_id,
                text,
                current_correlation_id(),
                external_message_id=None,
                idempotency_key=key,
            )
        logger.warning("usage_quiet_alert_sent", error_code=ERR_USAGE_QUIET_DAY)
        record_usage_quiet_alert("sent")
        return 1


def _quiet_text(tenant_id: uuid.UUID, tenant_name: str, local_now: datetime) -> str:
    settings = get_settings()
    return format_alert(
        error_code=ERR_USAGE_QUIET_DAY,
        title="тихий день — экзамен назначен, а к полудню никого",
        detail=(
            f"{tenant_name}: сегодня до {local_now:%H:%M} ни одного входа в кабинет, вопроса "
            "«Наставнику» или начатого экзамена. Сотрудникам не выдали ссылки, кабинет не "
            "открывается с их телефонов или об экзамене не объявили — что проверить, в статье"
        ),
        environment=settings.sentry_environment,
        runbook_url=settings.alert_runbook_url,
        emoji="🟡",
        tenant=str(tenant_id),
        correlation_id=current_correlation_id(),
    )
