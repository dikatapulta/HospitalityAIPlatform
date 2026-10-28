"""Адаптеры порта веб-пуша (spec 0050) и выбор по настройкам."""

from __future__ import annotations

from hospitality.integrations.web_push.fake import FakeWebPushAdapter
from hospitality.integrations.web_push.vapid import VapidWebPushAdapter
from hospitality.shared.config import Settings, staff_push_vapid_key_bytes
from hospitality.shared.ports.web_push import WebPushPort

__all__ = ["FakeWebPushAdapter", "VapidWebPushAdapter", "build_web_push"]


def build_web_push(settings: Settings) -> WebPushPort | None:
    """Адаптер по `STAFF_PUSH_BACKEND`; `None` — пуша нет (vapid без ключа).

    Зовёт только composition root. Негодный ключ сюда не доходит: его
    отвергает preflight до сборки процесса (`staff_push_vapid_key_bytes`).
    """
    if settings.staff_push_backend == "fake":
        return FakeWebPushAdapter()
    key = staff_push_vapid_key_bytes(settings)
    if key is None:
        return None
    return VapidWebPushAdapter(key, settings.staff_push_vapid_subject)
