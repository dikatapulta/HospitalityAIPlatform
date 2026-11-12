"""Ретеншн гостевых текстов (issue #42, spec 0032).

Опубликованная политика конфиденциальности обещает: тексты сообщений — 90 дней,
затем удаляются; свободный текст заявок обезличивается через 90 дней
(`docs/legal/privacy-policy.md` п. 7). Исполняет обещание этот модуль: воркер
раз в `worker_retention_interval_seconds` зовёт `enforce_guest_text_retention`,
которая по каждому тенанту:

1. удаляет `messages` старше `messages_retention_days`;
2. удаляет `unanswered_questions` того же возраста (spec 0036 §6): `question` —
   пересказ вопроса гостя моделью, то есть гостевой текст (PII_REGISTRY).
   Удаляется по СОБСТВЕННОМУ возрасту строки, а не каскадом за диалогом: живой
   диалог гостя, который пишет каждую неделю, каскада не даст никогда, а
   обещание политики дано на текст, а не на диалог;
3. удаляет `conversations`, давно не обновлявшиеся И оставшиеся без сообщений
   (каскадом уходят `request_origins`; согласие гостя — доказательная запись
   «как у диалога», PII_REGISTRY — умирает вместе с ним: вернувшийся после
   90+ дней тишины гость проходит consent-gate заново, это корректно);
4. обезличивает свободный текст заявок того же возраста через публичный API
   модуля requests (домен сам правит свою таблицу, spec 0028 §3);
5. удаляет строки журнала изменений кабинета того же возраста через функцию
   ядра (spec 0041 §2.5: своего срока у журнала нет — он тот же);
6. обнуляет замечания хаускипинга о номере того же возраста через публичный
   API модуля housekeeping (spec 0046 §8: замечание может пересказывать гостя);
7. удаляет файлы гостя того же возраста — документы и фото к заявке, файл и
   строку `uploaded_files` — через сервис ядра (spec 0047 §6; фото блюд и
   товаров не трогает: их срок — пока живёт позиция меню);
8. обезличивает запросы реквизитов для АВР того же возраста через публичный
   API модуля documents — шифртексты реквизитов и текста гостя обнуляются,
   строка живёт (spec 0049 §3), — и удаляет брошенные: запрос без заявки
   старше суток вместе с его файлами;
9. удаляет реплики диалогов «Наставника» и обнуляет их резюме того же возраста
   через публичный API модуля mentor (spec 0048 §4: вопрос сотрудника может
   назвать гостя или коллегу; срок у них с гостевым общий намеренно);
10. обезличивает заказы в номер того же возраста через публичный API модуля
   room_service — связь с проживанием и гостем обнуляется, состав и суммы
   остаются (spec 0038, ревью PR #497 Н-2; пожелания гостя — текст заявки,
   шаг 4), — и удаляет брошенные: заказ без заявки старше суток (Р-3);
11. обезличивает брони столика того же возраста через публичный API модуля
   table_booking — связь с проживанием и гостем обнуляется, ресторан, час и
   число гостей остаются (spec 0064 §3; имя и пожелания — текст заявки, шаг 4), —
   и удаляет брошенные: бронь без заявки старше суток;
12. обезличивает запросы из 3D-витрины через публичный API модуля showroom —
   контакты, компания и пожелания посетителя обнуляются через СВОЙ срок, 365 дней
   (spec 0069 §3: посетитель — не гость, сделка по группе живёт месяцами), —
   и удаляет брошенные: запрос без заявки старше суток;
13. обнуляет комментарии гостя к оценкам выполненных заявок того же возраста
   через публичный API модуля feedback — по времени комментария; оценка, служба
   и время остаются для сводки (spec 0076 §4.5);
14. удаляет служебные записки и объяснительные через публичный API модуля memos:
   черновики — того же возраста, отправленные — через СВОЙ срок отеля
   (`memo_retention_days`, spec 0077 §8) вместе с листом подписей и просмотрами;
15. удаляет данные о проживании через публичный API модуля guests — Stay, чей
   выезд старше того же срока, с кодами, ссылками и сессиями, и гостей без Stay
   с их идентичностями; у диалогов удалённой идентичности обнуляет ссылку на неё;
   записи о согласии живут СВОЙ срок, `consent_record_retention_days` (spec 0032
   §6). Последним шагом: ссылки заявок, заказов и броней на Stay к этому моменту
   обнулены шагами выше.

Механика — канон периодической задачи `cleanup_terminal_events` (ADR-009) и
канон обхода тенантов `remind_unclaimed_requests` (spec 0028 §4), но списком
ВСЕХ тенантов (`list_tenant_ids`): данные и юр-обязанность есть и у тенанта
без завершённого онбординга. Идемпотентность (P-8) — свойство самих операций:
повторный прогон не находит строк; гонка двух воркеров безопасна.

Мультитенантность (P-4): список тенантов берётся платформенной сессией, все
DELETE/UPDATE — внутри `tenant_context` каждого. Кросс-тенантного запроса к
бизнес-таблице здесь нет и быть не может.

Сторож обещания (issue #410, spec 0032 §5). Маркер `guest_text_retention_completed`
пишется и тогда, когда упали все тенанты, а «удалено 0 строк» неотличимо от
«нечего удалять», — поэтому снаружи смотрят не на него, а на сам результат:
`refresh_guest_text_age_metrics` отдаёт в `/metrics` возраст самого старого
сообщения каждого тенанта, и алертер (ERR-OPS-017) говорит, когда он перерос
срок. Прогон без единого сбоя отмечается строкой `guest-text-retention` в
`worker_heartbeats` — по ней рунбук видит, когда ретеншн последний раз прошёл
целиком.

Каждый шаг — в своём `try` (#1053 п. 5, `retention_steps.py`): сбой шага
одного раздела не отменяет остальные шаги отеля.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import structlog
from sqlalchemy import func, select

from hospitality.channels.common import retention_steps
from hospitality.channels.common.models import Message
from hospitality.channels.common.retention_steps import (
    ERR_CHANNEL_RETENTION_FAILED,
    RetentionRunStats,
)
from hospitality.platform.config import list_tenant_ids
from hospitality.shared.config import get_settings
from hospitality.shared.db import platform_session_scope, session_scope, utc_now
from hospitality.shared.heartbeat import record_worker_heartbeat
from hospitality.shared.logging import get_logger
from hospitality.shared.metrics import set_guest_text_oldest_ages
from hospitality.shared.tenancy import tenant_context

__all__ = [
    "ERR_CHANNEL_RETENTION_FAILED",
    "RETENTION_HEARTBEAT",
    "RetentionRunStats",
    "enforce_guest_text_retention",
    "refresh_guest_text_age_metrics",
]

logger = get_logger(module=__name__)

# Имя строки в `worker_heartbeats` (issue #410): прогон без единого сбоя.
RETENTION_HEARTBEAT = "guest-text-retention"

# Ретеншн одного отеля; имя модуля — точка подмены для тестов сбоя отеля целиком.
_enforce_tenant = retention_steps.enforce_tenant


async def enforce_guest_text_retention(retention_days: int | None = None) -> RetentionRunStats:
    """Один прогон ретеншна по всем тенантам; вернуть счётчики.

    Зовётся из цикла воркера (`hospitality/worker.py`) и тестами напрямую.
    Без явного `retention_days` берётся `messages_retention_days` из Settings
    (сигнатура — копия канона `cleanup_terminal_events`). Прогон получает
    собственный `correlation_id` (§10.2, канон spec 0028). Сбой на одном
    тенанте логируется ERR-CHANNEL-004 и не отменяет остальных; прогон без
    сбоев отмечается пульсом `RETENTION_HEARTBEAT` (issue #410).
    """
    if retention_days is None:
        retention_days = get_settings().messages_retention_days
    cutoff = utc_now() - timedelta(days=retention_days)
    with structlog.contextvars.bound_contextvars(correlation_id=uuid.uuid4().hex):
        async with platform_session_scope() as session:
            tenant_ids = await list_tenant_ids(session)

        totals = RetentionRunStats._make(
            [len(tenant_ids)] + [0] * (len(RetentionRunStats._fields) - 1)
        )
        failed = 0
        for tenant_id in tenant_ids:
            try:
                deleted = await _enforce_tenant(tenant_id, cutoff)
            except Exception:
                # Сбой вне шагов (контекст тенанта, недоступная в этот момент БД)
                # не отменяет ретеншн у остальных: обещание политики дано
                # каждому гостю, а не «пока всё работает».
                logger.error(
                    "guest_text_retention_failed",
                    error_code=ERR_CHANNEL_RETENTION_FAILED,
                    tenant_id=str(tenant_id),
                    exc_info=True,
                )
                failed += 1
                continue
            failed += 1 if deleted.steps_failed else 0
            totals = RetentionRunStats._make(
                total + part
                for total, part in zip(totals, deleted._replace(tenants=0), strict=True)
            )
        totals = totals._replace(tenants_failed=failed)
        # Нулевые счётчики отличают «нечего удалять» от «джоба не ходила»;
        # `tenants_failed` — «completed» пишется и при сбое (issue #410).
        logger.info(
            "guest_text_retention_completed", retention_days=retention_days, **totals._asdict()
        )
        if failed == 0:
            await record_worker_heartbeat(RETENTION_HEARTBEAT)
        return totals


async def refresh_guest_text_age_metrics() -> None:
    """Возраст самого старого сообщения каждого тенанта — в `/metrics` (issue #410).

    Обновлятор scrape: подключает `app.py` (`register_scrape_refresher`). Смотрит
    на результат, а не на маркер прогона: перерос срок — ретеншн не удаляет, по
    какой бы причине (исключение, неверный cutoff, RLS). Тенанты — все, как у
    самого прогона; без сообщений — 0. Запрос — на тенанта (RLS, ADR-003);
    недоступная БД стирает снимок целиком — для алертера это «не знаю».
    """
    try:
        async with platform_session_scope() as session:
            tenant_ids = await list_tenant_ids(session)
        now = utc_now()
        ages: dict[str, float] = {}
        for tenant_id in tenant_ids:
            with tenant_context(tenant_id):
                async with session_scope() as session:
                    oldest = await session.scalar(select(func.min(Message.created_at)))
            ages[str(tenant_id)] = 0.0 if oldest is None else (now - oldest).total_seconds()
    except Exception:  # диагностический путь: /metrics обязан отдаваться и без БД
        logger.warning("guest_text_age_unavailable", exc_info=True)
        set_guest_text_oldest_ages({})
        return
    set_guest_text_oldest_ages(ages)
