"""Квота бесплатного в диалоге гостя: исход проверки и тексты (spec 0056 §2, §4, §5).

Не инструмент — в реестре его нет. Это половина `create_service_request.prepare`,
вынесенная из канона, чтобы обёртка осталась тонкой: сколько можно бесплатно,
считает `requests.api.check_quota`, а здесь — что из этого следует для вызова
(служба, штуки, платное предложение) и что сказать гостю и сотруднику.

Модель только говорит, что просьба — по квоте (`quota_key`, `quota_units`);
предел, маршрут, цену и текст решает код (Р-4, Р-7): выдуманная цена — дефект P1
(FOUNDATION §7.4). Тексты — ru/kk/en, язык гостя другой — en, как у
`LocalizedText` (spec 0038 Р-6).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, TypeGuard

from hospitality.ai.tools.base import QUOTA_OFFER_ARGUMENT, PreparedCall, ToolTurnContext
from hospitality.modules.guests import api as guests_api
from hospitality.modules.requests import api as requests_api
from hospitality.modules.room_service import api as room_service_api
from hospitality.platform.service_quotas import MAX_QUOTA_UNITS, ServiceQuota
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

QUOTA_KEY_ARGUMENT: Final = "quota_key"
QUOTA_UNITS_ARGUMENT: Final = "quota_units"
# Предел `details` заявки — `CreateServiceRequestArgs.details`: строка кода
# встаёт первой, хвост модели режется, а не валит вызов.
_MAX_DETAILS_CHARS: Final = 4000
_TEXT_LANGUAGES: Final = frozenset({"ru", "kk", "en"})


class QuotaOutcome(enum.StrEnum):
    """Исход проверки — поле `outcome` лога `service_quota_checked` (spec 0056 §4)."""

    WITHIN = "within"
    PARTIAL = "partial"
    PAID_OFFER = "paid_offer"
    REFUSED = "refused"
    NO_STAY = "no_stay"


@dataclass(frozen=True)
class QuotaOffer:
    """Платное предложение сверх нормы, как его кладёт в аргументы `prepare`."""

    units: int
    unit_price_minor: int | None


@dataclass(frozen=True)
class _Texts:
    exhausted: str  # «{name}: бесплатно {limit} в день на ваш номер — на сегодня уже заказано.»
    paid_priced: str
    paid_unpriced: str
    no_paid: str
    partial: str
    partial_priced: str
    partial_unpriced: str
    partial_question: str
    done_priced: str
    done_unpriced: str


# Утверждены основателем 30.10 (ответ окну 4 на показ spec 0056 §5); строки
# «частично, цена не задана» на kk и en — достройка этого PR (docs/founder-review/).
_TEXTS: Final[dict[str, _Texts]] = {
    "ru": _Texts(
        exhausted="{name}: бесплатно {limit} в день на ваш номер — на сегодня уже заказано.",
        paid_priced="Могу оформить платно: {price_part}, оплата сотруднику при доставке. Оформить?",
        paid_unpriced=(
            "Могу оформить платно — сумму назовёт сотрудник, оплата при доставке. Оформить?"
        ),
        no_paid="Если нужно ещё, подскажут на ресепшене.",
        partial="{name}: бесплатно {limit} в день на ваш номер, на сегодня осталось {free}.",
        partial_priced="Сверх — платно, {price} за штуку.",
        partial_unpriced="Сверх — платно, сумму назовёт сотрудник.",
        partial_question="Оформить {free} бесплатно?",
        done_priced="Готово, оформил платно: {price_part}. Сотрудник принесёт и возьмёт оплату.",
        done_unpriced="Готово, оформил платно. Сотрудник принесёт и назовёт сумму.",
    ),
    "kk": _Texts(
        exhausted="{name}: нөміріңізге күніне {limit} тегін, бүгінгі мөлшерге тапсырыс берілді.",
        paid_priced=(
            "Ақылы түрде рәсімдей аламын — {price_part}, төлем жеткізгенде қызметкерге. "
            "Рәсімдейін бе?"
        ),
        paid_unpriced=(
            "Ақылы түрде рәсімдей аламын — бағасын қызметкер айтады, төлем жеткізгенде. "
            "Рәсімдейін бе?"
        ),
        no_paid="Тағы керек болса, ресепшенде көмектеседі.",
        partial="{name}: нөміріңізге күніне {limit} тегін, бүгін {free} қалды.",
        partial_priced="Қалғаны ақылы — данасы {price}.",
        partial_unpriced="Қалғаны ақылы — бағасын қызметкер айтады.",
        partial_question="{free} тегін рәсімдейін бе?",
        done_priced=(
            "Дайын, ақылы түрде рәсімдедім — {price_part}. Қызметкер әкеліп, төлемді алады."
        ),
        done_unpriced="Дайын, ақылы түрде рәсімдедім. Қызметкер әкеліп, бағасын айтады.",
    ),
    "en": _Texts(
        exhausted="{name}: {limit} free per day for your room, and today's are already ordered.",
        paid_priced=(
            "I can order more at {price_part}, paid to the staff member on delivery. Shall I?"
        ),
        paid_unpriced=(
            "I can order more — the staff member will tell you the price, paid on delivery. "
            "Shall I?"
        ),
        no_paid="If you need more, reception will help.",
        partial="{name}: {limit} free per day for your room, {free} left for today.",
        partial_priced="More are {price} each.",
        partial_unpriced="More are paid — the staff member will tell you the price.",
        partial_question="Shall I order {free} free of charge?",
        done_priced=(
            "Done — ordered at {price_part}. A staff member will bring it and take the payment."
        ),
        done_unpriced="Done — ordered. A staff member will bring it and tell you the price.",
    ),
}


async def prepare_quota(
    arguments: Mapping[str, Any], context: ToolTurnContext, *, language: str | None
) -> PreparedCall:
    """Проверить квоту до вопроса «оформить?» (spec 0056 Р-5) и поправить вызов.

    Модель не поставила ключ — вызов как сегодня. Кривой ключ или число — без
    квоты или 1 штука и WARNING, не эскалация: заявка ценнее метки (Р-4). Без
    проживания или с закрытым Stay квоты нет — заявка как сегодня, в счёт не
    идёт. Срочную заявку сюда не передают: гейт снят, квоту она не проходит.
    """
    prepared = {
        key: value
        for key, value in arguments.items()
        if key not in (QUOTA_KEY_ARGUMENT, QUOTA_UNITS_ARGUMENT)
    }
    raw_key = arguments.get(QUOTA_KEY_ARGUMENT)
    if raw_key is None:
        return PreparedCall(arguments=prepared)
    rule = next((quota for quota in context.service_quotas if quota.key == raw_key), None)
    if rule is None:
        logger.warning("service_quota_argument_invalid", argument=QUOTA_KEY_ARGUMENT)
        return PreparedCall(arguments=prepared)
    units = _units(arguments.get(QUOTA_UNITS_ARGUMENT))

    stay = None
    if context.verified_stay_id is not None:
        stay = await guests_api.get_active_stay(context.verified_stay_id)
    if stay is None:
        _log_check(rule, QuotaOutcome.NO_STAY)
        return PreparedCall(arguments=prepared)
    check = await requests_api.check_quota(
        rule, stay_id=stay.id, guests_count=stay.guests_count, units=units
    )
    texts = _TEXTS[_text_language(language)]
    name = rule.names.text_for(_text_language(language))
    # Заявка по квоте — в службу правила, а не в службу модели (§4): `execute` на
    # ходе «да» конфига не читает, поэтому служба едет в аргументах.
    _note_category_override(prepared, rule.category_key)

    if check.free_units == units:
        _log_check(rule, QuotaOutcome.WITHIN, check)
        return PreparedCall(arguments=_with_quota(prepared, rule, units))

    head_values = {"name": name, "limit": check.limit, "free": check.free_units}
    if check.free_units > 0:
        _log_check(rule, QuotaOutcome.PARTIAL, check)
        staff_line = f"Бесплатно по норме: {check.free_units} шт. из {units} запрошенных."
        return PreparedCall(
            arguments=_with_staff_line(_with_quota(prepared, rule, check.free_units), staff_line),
            question=_partial_question(texts, rule, head_values, _text_language(language)),
        )

    head = texts.exhausted.format(**head_values)
    if rule.paid is None:
        _log_check(rule, QuotaOutcome.REFUSED, check)
        return PreparedCall(arguments=prepared, refusal=f"{head} {texts.no_paid}")

    _log_check(rule, QuotaOutcome.PAID_OFFER, check)
    offer = QuotaOffer(units=units, unit_price_minor=rule.paid.unit_price_minor)
    paid_arguments = {
        **prepared,
        "category_key": rule.paid.category_key,
        QUOTA_OFFER_ARGUMENT: {"units": offer.units, "unit_price_minor": offer.unit_price_minor},
    }
    if offer.unit_price_minor is None:
        tail = texts.paid_unpriced
    else:
        tail = texts.paid_priced.format(price_part=_price_part(offer, _text_language(language)))
    return PreparedCall(
        arguments=_with_staff_line(paid_arguments, _paid_staff_line(offer)),
        question=f"{head} {tail}",
    )


def offer_from_arguments(arguments: Mapping[str, Any]) -> QuotaOffer | None:
    """Платное предложение из аргументов вызова; нет его или он кривой — None.

    Его пишет только `prepare` (модельное выброшено до гейта), поэтому кривое
    значение — защита на случай чужого `pending_action`, а не путь модели.
    """
    raw = arguments.get(QUOTA_OFFER_ARGUMENT)
    if not isinstance(raw, Mapping):
        return None
    units, price = raw.get("units"), raw.get("unit_price_minor")
    if not _is_units(units):
        return None
    if price is None:
        return QuotaOffer(units=units, unit_price_minor=None)
    if not _is_int(price) or price <= 0:
        return None
    return QuotaOffer(units=units, unit_price_minor=price)


def paid_done_text(offer: QuotaOffer, language: str | None) -> str:
    """Реплика на «да» платному (§5) — её даёт код, а не классификатор (Р-7)."""
    text_language = _text_language(language)
    texts = _TEXTS[text_language]
    if offer.unit_price_minor is None:
        return texts.done_unpriced
    return texts.done_priced.format(price_part=_price_part(offer, text_language))


def _partial_question(
    texts: _Texts, rule: ServiceQuota, values: dict[str, Any], language: str
) -> str:
    parts = [texts.partial.format(**values)]
    if rule.paid is not None:
        price = rule.paid.unit_price_minor
        parts.append(
            texts.partial_unpriced
            if price is None
            else texts.partial_priced.format(price=room_service_api.format_amount(price, language))
        )
    parts.append(texts.partial_question.format(**values))
    return " ".join(parts)


def _price_part(offer: QuotaOffer, language: str) -> str:
    """`500 ₸` за одну штуку, `1 000 ₸ (2 × 500 ₸)` за несколько (§5)."""
    assert offer.unit_price_minor is not None
    unit = room_service_api.format_amount(offer.unit_price_minor, language)
    if offer.units == 1:
        return unit
    total = room_service_api.format_amount(offer.unit_price_minor * offer.units, language)
    return f"{total} ({offer.units} × {unit})"


def _paid_staff_line(offer: QuotaOffer) -> str:
    """Первая строка `details` платной заявки — кабинет русский (§5)."""
    if offer.unit_price_minor is None:
        return (
            f"Платно, сверх бесплатной нормы: {offer.units} шт., цену назовите гостю при доставке."
        )
    unit = room_service_api.format_amount(offer.unit_price_minor, "ru")
    total = room_service_api.format_amount(offer.unit_price_minor * offer.units, "ru")
    return f"Платно, сверх бесплатной нормы: {offer.units} × {unit} = {total}, оплата при доставке."


def _with_quota(arguments: dict[str, Any], rule: ServiceQuota, units: int) -> dict[str, Any]:
    return {
        **arguments,
        "category_key": rule.category_key,
        QUOTA_KEY_ARGUMENT: rule.key,
        QUOTA_UNITS_ARGUMENT: units,
    }


def _with_staff_line(arguments: dict[str, Any], line: str) -> dict[str, Any]:
    """Строка кода — первой в `details`: сотрудник видит число и цену кода, а не
    «3 бутылки воды» модели (ревью PR #562), и реплика классификатора — тоже."""
    model_details = str(arguments.get("details") or "").strip()
    details = f"{line}\n{model_details}" if model_details else line
    return {**arguments, "details": details[:_MAX_DETAILS_CHARS]}


def _note_category_override(arguments: Mapping[str, Any], category_key: str) -> None:
    model_category = arguments.get("category_key")
    if model_category != category_key:
        logger.info(
            "service_quota_category_overridden",
            model_category=str(model_category),
            quota_category=category_key,
        )


def _units(value: object) -> int:
    """Штук по квоте: не названо — 1; кривое число — 1 и WARNING (Р-4)."""
    if value is None:
        return 1
    if _is_units(value):
        return value
    logger.warning("service_quota_argument_invalid", argument=QUOTA_UNITS_ARGUMENT)
    return 1


def _is_int(value: object) -> TypeGuard[int]:
    # bool — подкласс int: `true` модели не «одна штука».
    return isinstance(value, int) and not isinstance(value, bool)


def _is_units(value: object) -> TypeGuard[int]:
    return _is_int(value) and 1 <= value <= MAX_QUOTA_UNITS


def _text_language(language: str | None) -> str:
    return language if language in _TEXT_LANGUAGES else "en"


def _log_check(
    rule: ServiceQuota, outcome: QuotaOutcome, check: requests_api.QuotaCheck | None = None
) -> None:
    # `stay_id` в лог не идёт (spec 0054): связь с человеком — только в БД.
    logger.info(
        "service_quota_checked",
        quota_key=rule.key,
        limit=None if check is None else check.limit,
        used=None if check is None else check.used,
        outcome=outcome.value,
    )
