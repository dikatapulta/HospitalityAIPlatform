"""Уведомления основателю о суперадмине — подписчик `SuperadminActed` (spec 0044 §6).

Событие публикуют вход и действия суперадмина в той же транзакции, что само
действие (`staff_superadmin`, `tools/superadmin`); этот подписчик воркера
превращает его в сообщение алерт-бота (`shared/alerting`, пара
`TELEGRAM_ALERT_*` — у воркера она есть, у приложения нет).

В тексте — отель, время по отелю и роль сотрудника; логинов и имён нет: текст
уходит в Telegram за границу (закон о ПД РК, ст. 12; правило 2 PII_REGISTRY).
Кого именно — по `user_id` в логе, порядок — рунбук `superadmin.md`.

Повтор доставки — повтор сообщения: дубль уведомления безвреден, пропуск —
нет, поэтому идемпотентность (P-8) здесь — at-least-once, без ключа эффекта.
Алерт-бот не настроен (dev, CI) — WARNING и событие закрыто: человеку сказать
нечем, а копить событие до настройки значило бы получить пачку старых
уведомлений в день, когда бот появится.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, tzinfo
from typing import Final

from hospitality.platform.config import load_tenant_config, load_tenant_name
from hospitality.platform.events import SuperadminActed, SuperadminActionKind
from hospitality.platform.models import StaffRole
from hospitality.shared.alerting import AlertSender
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope
from hospitality.shared.errors import AppError
from hospitality.shared.logging import get_logger
from hospitality.shared.tenancy import current_tenant_id

logger = get_logger(module=__name__)

# Подписи ролей — те же, что у кабинета (`staff_portal/team.ROLE_LABELS`):
# ядро не импортирует слой страниц, совпадение держит тест
# `test_superadmin_alert_role_names_match_cabinet`.
ROLE_NAMES: Final[dict[StaffRole, str]] = {
    StaffRole.STAFF: "Сотрудник",
    StaffRole.SENIOR: "Старший службы",
    StaffRole.MANAGER: "Менеджер отеля",
}

RUNBOOK_ANCHOR: Final = "#если-вход-или-действие-не-ваши"


def runbook_url() -> str:
    """Рунбук суперадмина рядом с рунбуком алертов (`ALERT_RUNBOOK_URL`)."""
    base = get_settings().alert_runbook_url.rsplit("/", 1)[0]
    return f"{base}/superadmin.md{RUNBOOK_ANCHOR}"


def _what(event: SuperadminActed) -> tuple[str, str]:
    """Значок и суть сообщения по виду события."""
    target = event.target_role
    role = f"«{ROLE_NAMES.get(target, target.value)}»" if target is not None else "—"
    texts: dict[SuperadminActionKind, tuple[str, str]] = {
        SuperadminActionKind.LOGIN: ("🔐", "Вход суперадмина"),
        SuperadminActionKind.LOGIN_BAD_CODE: (
            "⚠️",
            "Суперадмин: пароль верный, код из приложения — нет",
        ),
        SuperadminActionKind.PASSWORD_RESET_ISSUED: (
            "🔑",
            f"Суперадмин выдал ссылку сброса пароля сотруднику с ролью {role}",
        ),
        SuperadminActionKind.MEMBER_ERASED: ("🗑", f"Суперадмин стёр учётку с ролью {role}"),
        SuperadminActionKind.CREATED_ON_SERVER: ("🛠", "На сервере заведён суперадмин"),
        SuperadminActionKind.FACTOR_RESET_ON_SERVER: (
            "🛠",
            "На сервере заменён код из приложения суперадмина",
        ),
        SuperadminActionKind.PASSWORD_SET_ON_SERVER: (
            "🛠",
            "На сервере сменён пароль суперадмина",
        ),
    }
    return texts[event.kind]


def format_superadmin_alert(
    event: SuperadminActed, *, hotel: str, zone: tzinfo, environment: str, runbook: str
) -> str:
    """Текст уведомления — чистая функция (тесты сверяют его без сети)."""
    emoji, what = _what(event)
    moment = event.occurred_at.astimezone(zone).strftime("%d.%m %H:%M")
    if event.kind is SuperadminActionKind.LOGIN_BAD_CODE:
        tail = f"Если это не вы, пароль знает кто-то ещё: {runbook}"
    else:
        tail = f"Не вы? {runbook}"
    return f"{emoji} [{environment}] {what} — «{hotel}», {moment} по времени отеля.\n{tail}"


async def _hotel_and_zone() -> tuple[str, tzinfo]:
    tenant_id = current_tenant_id()
    async with platform_session_scope() as session:
        hotel = await load_tenant_name(session, tenant_id)
        try:
            zone: tzinfo = (await load_tenant_config(session, tenant_id)).tzinfo
        except AppError:
            # Онбординг не завершён — пояса нет; время честно в UTC, чем никак.
            zone = UTC
    return hotel, zone


def build_superadmin_alert_handler(
    alert_sender: AlertSender | None,
) -> Callable[[SuperadminActed], Awaitable[None]]:
    """Подписчик для `register_subscribers` воркера; отправитель — инъекцией."""

    async def notify_superadmin_acted(event: SuperadminActed) -> None:
        if alert_sender is None:
            logger.warning(
                "superadmin_alert_skipped",
                reason="TELEGRAM_ALERT_BOT_TOKEN/TELEGRAM_ALERT_CHAT_ID не заданы",
                kind=event.kind.value,
                actor_user_id=str(event.actor_user_id),
            )
            return
        hotel, zone = await _hotel_and_zone()
        text = format_superadmin_alert(
            event,
            hotel=hotel,
            zone=zone,
            environment=get_settings().sentry_environment,
            runbook=runbook_url(),
        )
        await alert_sender(text)
        logger.info(
            "superadmin_alert_sent",
            kind=event.kind.value,
            actor_user_id=str(event.actor_user_id),
            target_user_id=str(event.target_user_id) if event.target_user_id else None,
        )

    return notify_superadmin_acted
