"""Пульс алертера наружу (issue #229): «сторож жив и сторожит».

Алертер стережёт приложение, а его самого не стережёт никто: упал процесс,
крутится в рестарт-лупе на полузаполненной паре ``TELEGRAM_ALERT_*``, стоит в
пассивном режиме или умер весь сервер — алерты молчат, и это выясняется ровно
тогда, когда они были нужны (аудит 05.08, Д-1). О своей смерти процесс доложить
не может — тот же довод, что у пульса воркера (``shared/heartbeat.py``), — поэтому
сравнивает с часами ДРУГОЙ сторож, и он вне сервера: проверка внешнего сервиса
(healthchecks.io) ждёт запрос на свой адрес и, не дождавшись, пишет основателю сама.

Пульс шлёт только активный цикл, после полного круга опроса. Пассивный режим
(обе ``TELEGRAM_ALERT_*`` пустые) пульса не шлёт намеренно: «алерты выключены»
снаружи выглядит так же, как «алертер мёртв», и для того, кто ждёт алертов,
это одно и то же.

Адрес проверки — секрет: с ним кто угодно держал бы проверку зелёной за мёртвого
алертера. Поэтому в лог не идёт ни он, ни текст исключения httpx (у
``HTTPStatusError`` в тексте полный адрес) — только класс ошибки или код ответа.

Сбой пульса — ERROR один раз на вход в сбой (событие Sentry), выход — INFO;
сам сбой алертом не становится: недошедший пульс — это и есть тревога
внешнего сервиса. Немым остался бы только неверный адрес — проверка, не
получившая ни одного пульса, стоит в статусе New, а тревога healthchecks.io —
это переход в Down. На неизвестный адрес сервис отвечает не 404, а
``200 OK (not found)`` (docs/http_api), поэтому успех — это тело ``OK``, а не код 200.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Код каталога (docs/runbooks/errors.md, R-8): только лог-событие, как ERR-OPS-005.
ERR_HEARTBEAT_FAILED = "ERR-OPS-011"

# healthchecks.io просит не чаще 5 пульсов в минуту на проверку, лишние отвечает
# «OK (rate limited)» и не записывает. Раз в минуту — с запасом при любом
# ALERT_POLL_INTERVAL_SECONDS.
MIN_INTERVAL_SECONDS = 60.0

_RECORDED_BODY = "OK"


@dataclass
class HeartbeatPinger:
    """Пульс на адрес внешней проверки; пустой ``url`` — пульса нет (dev/CI)."""

    url: str
    min_interval_seconds: float = MIN_INTERVAL_SECONDS
    last_ping_at: float | None = None
    failing: bool = False

    def maybe_ping(self, client: httpx.Client, *, now: float) -> None:
        """Отправить пульс, если с прошлого прошло не меньше ``min_interval_seconds``.

        ``now`` — монотонные часы цикла. Сбой не бросается: пульс — побочная
        линия, и цикл опроса из-за неё не встанет.
        """
        if not self.url:
            return
        if self.last_ping_at is not None and now - self.last_ping_at < self.min_interval_seconds:
            return
        self.last_ping_at = now
        reason = self._ping(client)
        if reason is None:
            if self.failing:
                logger.info("alerter_heartbeat_recovered")
            self.failing = False
            return
        if not self.failing:
            logger.error(
                "alerter_heartbeat_failed",
                error_code=ERR_HEARTBEAT_FAILED,
                reason=reason,
                runbook="docs/runbooks/errors.md#err-ops-011",
            )
        self.failing = True

    def _ping(self, client: httpx.Client) -> str | None:
        """Один пульс; ``None`` — записан, иначе причина без адреса."""
        try:
            response = client.get(self.url)
        except httpx.HTTPError as error:
            return type(error).__name__
        if not response.is_success:
            return f"HTTP {response.status_code}"
        body = response.text.strip()
        if body != _RECORDED_BODY:
            return f"пульс не записан: {body[:40]}"
        return None
