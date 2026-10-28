# integrations/web_push — адаптеры веб-пуша

Реализации порта `WebPushPort` (`shared/ports/web_push.py`,
[spec 0050](../../../../docs/specs/0050-staff-web-push.md)): уведомления персоналу на
телефон. Подключает только composition root — `app.py` (и `worker.py` с рассылкой,
PR 2) зовут `build_web_push(settings)` и кладут результат в порт (`set_web_push`);
`None` — пуша на контуре нет. Ядро и модули адаптеры не импортируют (контракт 2
import-linter).

## Что умеет

Отправляет непрозрачные байты на адрес подписки браузера и говорит, чем кончилось:
`delivered` (2xx), `gone` (404/410), `rejected` (иной 4xx), `failed` (5xx, 429,
таймаут, сеть). О сотрудниках и текстах не знает — это `platform/staff_push.py`.

| Адаптер | Файл | Куда шлёт | Для чего |
|---|---|---|---|
| `FakeWebPushAdapter` | `fake.py` | в память процесса (`sent`); исход адреса — `set_outcome` | тесты, CI, локальный uvicorn (ADR-007) |
| `VapidWebPushAdapter` | `vapid.py` + `encryption.py` | службы пуша браузеров (Google, Apple, Mozilla, Microsoft) | staging, гостевая копия, прод |

VAPID-адаптер шифрует сообщение под ключи подписки (RFC 8291, `aes128gcm`, одна
запись, свежие ключ сервера и соль на каждое сообщение) и подписывает запрос JWT
ES256 (RFC 8292: `aud` — источник адреса, `exp` — +12 ч, `sub` — контакт). Всё на
`cryptography`; `pywebpush` не взят — тянет `aiohttp` и `requests` (spec 0050 §5).

## Конфигурация

| Переменная | Значение |
|---|---|
| `STAFF_PUSH_BACKEND` | `fake` (умолчание кода) или `vapid` (compose задаёт сам) |
| `STAFF_PUSH_VAPID_PRIVATE_KEY` | секрет: 32 байта base64url; пусто при `vapid` — пуша нет. Генерация — `python -m hospitality.tools.generate_vapid_key` |
| `STAFF_PUSH_VAPID_SUBJECT` | контакт для служб пуша, `mailto:support@necturn.com` |

Ключ проверяет preflight до старта процесса; где хранить и как менять —
[secrets.md](../../../../docs/runbooks/secrets.md), включение на контуре —
[staff-push.md](../../../../docs/runbooks/staff-push.md).

## Лимиты

- Тело — до 3993 байт открытого текста (RFC 8291 §4); больше — `ValueError` до сети.
- Адрес — только белый список хостов служб пуша, `https://` без порта
  (`validate_push_endpoint`): иначе сервер стучался бы по адресу из браузера (SSRF).
- Таймаут запроса — 10 с, без редиректов; `Urgency: high` — иначе Android в режиме
  сна откладывает доставку.
- Квот у служб пуша для нашего объёма нет; 429 — исход `failed`, повтор позже.

## Health и метрики

- `check()` — ключ загружен; у Fake всегда `True`. В `/health/ready` не входит
  намеренно: сломанный пуш не должен снимать с трафика кабинет и гостевой чат.
- Метрика `staff_push_sends_total{outcome}` пишется сервисом `platform/staff_push.py`
  вокруг вызова порта — одна на оба адаптера.

## Тесты

Один контрактный набор на оба адаптера — `tests/test_web_push_port.py` (R-7):
VAPID ходит через `httpx.MockTransport`, шифрование сверено с вектором RFC 8291
(приложение A), подпись JWT проверяется публичным ключом, тело — расшифровкой.
