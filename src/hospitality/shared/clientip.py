"""CANONICAL: настоящий адрес клиента за доверенным прокси (issue #207, P-12).

Единственное место, где приложение решает, «кто к нам пришёл»: адрес нужен
ключом rate-limit'ов (вход в кабинет, потребление QR-ссылки гостя), и цена
ошибки — не в точности, а в том, что весь отель схлопывается в один ключ.

За именованным Cloudflare-туннелем uvicorn видит адрес СОСЕДНЕГО КОНТЕЙНЕРА
(cloudflared по docker-сети), а не сотрудника: 60–100 человек в день раздачи
доступов делят один бюджет попыток и упираются в лимит на одиннадцатом
(находка А-2 операционного аудита 05.08).

Заголовку верим только от доверенного прокси (`TRUSTED_PROXY_IPS` —
`shared/config.py`): заголовок присылает кто угодно, и без проверки соседа
подделка `CF-Connecting-IP` обходила бы любой лимит по адресу. Проверка
опирается на адрес СОКЕТА — его подделать нельзя, но лишь пока его не
переписал сам сервер. А uvicorn переписывает его ПО УМОЛЧАНИЮ: параметр
`proxy_headers` объявлен как `default=True`, и `ProxyHeadersMiddleware`
встаёт в стек без всяких флагов. Инертным его держит второй дефолт —
`forwarded_allow_ips=127.0.0.1`, под который адрес cloudflared в docker-сети
не попадает; но этот дефолт сдвигается переменной окружения
`FORWARDED_ALLOW_IPS`, тоже без флага и без правки команды. Тогда
`scope["client"]` приходит из `X-Forwarded-For` (при `*` — САМЫМ ЛЕВЫМ
элементом, то есть присланным клиентом), и проверка доверия начинает
проверять подделку. Поэтому прослойка выключена ЯВНО — `--no-proxy-headers`
во ВСЕХ ТРЁХ местах запуска (`ops/Dockerfile` — оба CMD, `ops/deploy/docker-compose.staging.yml`,
`.claude/skills/verify/SKILL.md`): только с флагом «адрес сокета настоящий» верно по
построению, а не по совпадению дефолтов (находка ревью PR #244). Локальный запуск закрыт
тем же флагом не ради симметрии: там дефолт `forwarded_allow_ips=127.0.0.1` совпадает с
соседом по сокету, поэтому подделка проходит, и проверка этого файла руками без флага
показала бы картину, обратную staging (issue #248).

Разбирается ровно один заголовок — `CF-Connecting-IP`: его выставляет край
Cloudflare поверх всего, что прислал клиент, и это одно значение, а не список
с неоднозначным «чей элемент настоящий», как у `X-Forwarded-For`. Появится
другой вход (не Cloudflare) — его правило добавляется здесь, в одном месте.
"""

from __future__ import annotations

import ipaddress

from fastapi import Request

from hospitality.shared.config import get_settings
from hospitality.shared.logging import get_logger

logger = get_logger(module=__name__)

# Заголовок Cloudflare с адресом исходного клиента (docs Cloudflare
# «HTTP request headers»): край переписывает его на каждом запросе.
CLIENT_IP_HEADER = "CF-Connecting-IP"

# Адреса нет (ASGI-транспорт тестов не открывает сокет): ключ rate-limit
# обязан быть непустым, поэтому явная константа, а не пустая строка.
UNKNOWN_CLIENT_IP = "unknown"


def _is_trusted_proxy(peer: str) -> bool:
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        # Не адрес (например, `unknown` тестового транспорта) — доверия нет.
        return False
    return any(address in network for network in get_settings().trusted_proxy_ips)


def client_ip(request: Request) -> str:
    """Адрес клиента запроса — канон для ключей rate-limit; в лог — `client_network`.

    Возвращает `CF-Connecting-IP`, если запрос пришёл от доверенного прокси и
    заголовок — валидный IP; иначе адрес сокета; иначе `unknown`.
    """
    peer = request.client.host if request.client else None
    forwarded = request.headers.get(CLIENT_IP_HEADER)
    if forwarded is None:
        return peer or UNKNOWN_CLIENT_IP
    if peer is None or not _is_trusted_proxy(peer):
        # Не тишина: так выглядит и подделка снаружи, и наш собственный
        # промах — прокси сменил подсеть, `TRUSTED_PROXY_IPS` остался
        # прежним. Без этой записи второй случай неотличим от «всё хорошо»,
        # а лимит по адресу тихо возвращается к дефекту #207.
        logger.warning("client_ip.untrusted_proxy_header", peer=peer or UNKNOWN_CLIENT_IP)
        return peer or UNKNOWN_CLIENT_IP
    try:
        # Нормализация заодно валидирует: в ключ Redis не должно попасть
        # ничего, кроме адреса (мусор из заголовка исказил бы ключ).
        return str(ipaddress.ip_address(forwarded.strip()))
    except ValueError:
        logger.warning("client_ip.malformed_proxy_header", peer=peer)
        return peer


# Маска адреса для логов (#173): сеть /24 у IPv4 и /48 у IPv6 — то же
# обнуление хвоста, что у «анонимизации IP» Google Analytics.
_IPV4_LOG_PREFIX = 24
_IPV6_LOG_PREFIX = 48


def mask_address(address: str) -> str:
    """Адрес → его сеть для лога: `203.0.113.9` → `203.0.113.0/24`.

    Адрес клиента — персональные данные, а в логах ПД маскируются (FOUNDATION
    §10, правило 2 `docs/PII_REGISTRY.md`). Сети хватает на вопрос расследования
    «одна сеть раз за разом или много разных» и на сверку с сетью отеля, ключа
    для неё не нужно — в отличие от хэша, который без секрета IPv4 не прячет.
    Не адрес (`unknown` тестового транспорта) — `unknown`: в лог не уходит
    ничего, кроме сети.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return UNKNOWN_CLIENT_IP
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    prefix = _IPV4_LOG_PREFIX if parsed.version == 4 else _IPV6_LOG_PREFIX
    return str(ipaddress.ip_network(f"{parsed}/{prefix}", strict=False))


def client_network(request: Request) -> str:
    """Сеть клиента запроса — поле `client_network` в логах, не сам адрес."""
    return mask_address(client_ip(request))
