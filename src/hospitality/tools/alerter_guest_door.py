"""Гости упираются в лимит у входа в чат — восьмая линия алертера (issue #415, ERR-OPS-014).

В чат гость входит одной из двух дверей: ссылкой с листка
(`guest_bind_link_consume`) или кодом комнаты (`guest_code_verify_ip` — по
адресу, `guest_code_verify` — по комнате). У каждой свой rate-limit, и его
отказ — HTTP 429 «слишком много попыток, обратитесь на ресепшен»: в чат гость
не попал. Отказ пишется на WARNING (в Sentry не уходит), а 429 — не 5xx
(ERR-OPS-002 молчит), поэтому до этой линии о стене владелец узнавал только
от гостей у стойки.

Гости в Wi-Fi отеля выходят в интернет с одного адреса: стена по адресу — это
сразу все, кто сейчас заселяется. Отказ по комнате — подбор кода или гость,
который не может ввести свой. Лимиты чата на том же счётчике
(`guest_chat_*`, `guest_message_length`) сюда не входят: это гость, который
пишет слишком много, — в чат он уже попал.

Устроена как ERR-OPS-002: прирост счётчика ``guest_rate_limited_total`` по
этим scope за интервал опроса ≥ ``ALERT_GUEST_DOOR_THRESHOLD`` — алерт, не
чаще ``ALERT_COOLDOWN_SECONDS``; ✅ нет — окно лимита открывается само.
Порог по умолчанию — один отказ: исправная ссылка бюджет не тратит (#415), и
любой отказ у двери — либо гость, который не попал в чат, либо чужой подбор.
"""

from __future__ import annotations

# Код каталога (docs/runbooks/errors.md, R-8).
ERR_GUEST_DOOR_RATE_LIMITED = "ERR-OPS-014"

_METRIC_PREFIX = "guest_rate_limited_total{"

# scope лимита → как дверь названа в тексте алерта.
GUEST_DOOR_SCOPES: dict[str, str] = {
    "guest_bind_link_consume": "ссылка с листка",
    "guest_code_verify_ip": "код комнаты, лимит адреса",
    "guest_code_verify": "код комнаты, лимит комнаты",
}


def read_guest_door_rejections(metrics_text: str) -> dict[str, float]:
    """Отказы у входа в чат из выдачи ``/metrics``: scope → сумма по тенантам.

    Строка выдачи — ``guest_rate_limited_total{tenant_id="…",scope="…"} 3.0``.
    Дверь без отказов присутствует нулём: счётчик с метками до первого отказа
    в выдаче не появляется, и «нет строки» здесь значит «ноль», а не «не знаю»
    («не знаю» — недоступный ``/metrics``, его разбирает вызывающий).
    """
    totals = dict.fromkeys(GUEST_DOOR_SCOPES, 0.0)
    for line in metrics_text.splitlines():
        if not line.startswith(_METRIC_PREFIX):
            continue
        labels, _, raw_value = line[len(_METRIC_PREFIX) :].rpartition("} ")
        scope = labels.partition('scope="')[2].partition('"')[0]
        if scope in totals:
            totals[scope] += float(raw_value)
    return totals


def door_rejections_delta(
    previous: dict[str, float], current: dict[str, float]
) -> dict[str, float]:
    """Прирост отказов по каждой двери между двумя опросами.

    Значение меньше прежнего — счётчик обнулился (приложение перезапустилось):
    приростом считается накопленное с нуля, как у ERR-OPS-002.
    """
    delta: dict[str, float] = {}
    for scope, value in current.items():
        before = previous.get(scope, 0.0)
        delta[scope] = value if value < before else value - before
    return delta


def describe_door_rejections(delta: dict[str, float]) -> str:
    """«+4 отказа 429 за интервал опроса: ссылка с листка — 3, …» — детали алерта."""
    parts = [f"{GUEST_DOOR_SCOPES[scope]} — {count:g}" for scope, count in delta.items() if count]
    return f"+{sum(delta.values()):g} отказа(ов) 429 за интервал опроса: {', '.join(parts)}"
