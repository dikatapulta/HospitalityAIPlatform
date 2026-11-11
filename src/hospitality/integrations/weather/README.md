# integrations/weather — адаптеры погоды

Реализации порта `WeatherPort` (`shared/ports/weather.py`,
[spec 0084](../../../../docs/specs/0084-concierge-weather-and-rates.md)). Подключает только
composition root — `build_weather(settings)` и `set_weather` (PR 2 серии); ядро и модули
адаптеры не импортируют (контракт 2 import-linter).

## Что умеет

`forecast(point) -> WeatherForecast` — почасовой прогноз от текущего часа в точке
`GeoPoint`; `check()` — отвечает ли провайдер. Нет прогноза — `WeatherUnavailableError`.

| Адаптер | Файл | Для чего |
|---|---|---|
| `FakeWeatherAdapter` | `fake.py` | тесты, CI, локальный uvicorn (ADR-007): без настройки — выдуманные 48 часов около 5 °C для любой точки; `set_forecast` — свой прогноз; `unavailable = True` — провайдер лежит |
| `MetNorwayWeatherAdapter` | `met_norway.py` | контуры: MET Norway Locationforecast 2.0 `compact` |

## Конфигурация

| Переменная | Значение |
|---|---|
| `WEATHER_BACKEND` | `fake` (умолчание кода) или `met_norway` |
| `WEATHER_USER_AGENT` | имя приложения и контакт; умолчание — `necturn-hospitality/1.0 necturn.com support@necturn.com` |

Ключа нет. Точку берёт вызывающий из конфига отеля (spec 0084 Р-4).

## Условия MET Norway (сверены 11.11.2026)

[TermsOfService](https://api.met.no/doc/TermsOfService), [License](https://api.met.no/doc/License):

- **Коммерческое применение** разрешено: данные под NLOD 2.0 и CC BY 4.0. Условие — указать
  источник («Data from MET Norway»): это делает бот в ответе гостю (spec 0084 §4). Имя
  сервиса не должно выдавать его за Yr, NRK или MET.
- **User-Agent** — обязателен и опознаёт приложение с контактом; выдуманный или чужой — бан
  навсегда, пустой — 403. Адаптер без него не создаётся.
- **Частота** — до 20 запросов в секунду на всё приложение; повтор не раньше `Expires`, с
  `If-Modified-Since` = прежний `Last-Modified`. 429 — снизить частоту сразу.
- **Координаты** — не больше 4 знаков после запятой (5 — 403): округляет `GeoPoint`.
- **203** — версия продукта устарела, через месяц её выключат: WARNING
  `weather_provider_version_deprecated` — повод обновить адрес.

## Кэш, отказы, лимиты

- Копия на точку — до `Expires` (без заголовка — 30 мин; рамки — от 1 мин до 6 ч), затем
  запрос с `If-Modified-Since`; ответ 304 продлевает копию.
- Таймаут — 3 с (ответа ждёт гость). Ответ больше 2 МБ — отказ.
- Отказ (нет ответа, не 200/203/304, не тот формат) — ERR-CONCIERGE-001: пауза 60 с для
  всех точек, копия моложе 3 часов отдаётся, старше — `WeatherUnavailableError`.
- Закончившиеся часы отбрасываются (`without_finished_hours` порта) — у обоих адаптеров.

## Health и метрики

- `check()` — `GET …/locationforecast/2.0/status` → 200; Fake — `not unavailable`. В
  `/health/ready` не входит: без погоды консьерж работает (FOUNDATION §8 правило 2).
- `concierge_source_calls_total{source="weather", outcome}` — каждый запрос: `ok`,
  `not_modified`, `error`. Ответ из копии запросом не считается.

## Тесты

Один контрактный набор на оба адаптера — `tests/test_weather_port.py` (R-7): MET говорит с
`httpx.MockTransport`, который отвечает форматом compact из состояния Fake, — прогнозы
обязаны совпасть. Там же — условия провайдера, отказы, пауза и прежняя копия.
