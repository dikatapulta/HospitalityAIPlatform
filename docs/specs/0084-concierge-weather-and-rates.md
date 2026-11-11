# Spec 0084: консьерж — погода и официальный курс валют (черновик Phase 2)

- **Статус:** реализуется — PR 1 (#1017): порты, адаптеры, контрактные тесты; PR 2 — блок хода
  и включение
- **Issue:** #951 — слово основателя 10.11.2026: «можешь задачи с фаз 3-4 брать … погода, курс
  валют … пусть как-то основу делают в качестве черновика»
- **Дата:** 2026-11-11

## 1. Проблема

Погода и курс тенге — первые вопросы иностранного гостя. Сегодня бот на них честно отвечает
«не знаю» и отправляет на ресепшен (spec 0036 §6) — справочник отеля этого не знает и знать не
должен: данные меняются каждый час. FOUNDATION §8 называет `WeatherPort` портом ядра, план
ставит его в Phase 2; основатель перенёс его черновиком (план, Phase 1, «Перенесено»).

## 2. Решения

**Р-1. Блок хода, а не инструмент.** Погода и курсы приходят модели блоком системного промпта
`# Weather and exchange rates` — так же, как свободные ночи `# Staying longer` (spec 0059 §2.4).
Issue предлагал инструмент «только чтение» по канону `create_service_request`, но ход
оркестратора однократный: у шлюза нет `tool_result`, а инструмент «узнать и ответить» требовал
бы второго обращения к модели на ходу — новой механики шлюза и оркестратора (ADR) и вдвое
дороже такой реплики. Блок стоит ~250 входных токенов на ход (~$0,0005 у Sonnet 5 по
`MODEL_PRICING_USD_PER_MTOK`, $2 за миллион) и только при включённом флаге. Решение за основателя —
[founder-review](../founder-review/2026-11-11-pr1017-concierge-weather.md).

**Р-2. Погода — MET Norway, курс — Национальный банк РК.** Оба открыты и без ключа:

- MET Norway Locationforecast 2.0 (`api.met.no/weatherapi/locationforecast/2.0/compact`):
  лицензии NLOD 2.0 и CC BY 4.0 — коммерческое применение разрешено, с указанием источника
  («Data from MET Norway»). Условия сверены 11.11.2026 по `api.met.no/doc/TermsOfService` и
  `api.met.no/doc/License`: опознающий `User-Agent` с адресом сайта; не чаще `Expires`; повтор —
  с `If-Modified-Since`; координаты — не больше 4 знаков (5 — ответ 403); всего до 20 запросов
  в секунду на приложение; 429 — сразу снизить частоту. Open-Meteo отвергнут: бесплатный API —
  только для некоммерческого применения (`open-meteo.com/en/terms`, 11.11.2026).
- НБ РК: `nationalbank.kz/rss/get_rates.cfm?fdate=ДД.ММ.ГГГГ`, XML (`<item>`: `title` — код
  ISO 4217, `description` — тенге за `quant` единиц). Курс складывается на KASE в 15:30 и
  действует со следующего дня; курс есть и на выходные, на завтрашнюю дату до публикации ответ
  пуст. Условия сверены 11.11.2026 по `nationalbank.kz/ru/page/data-usage-terms`: использование
  свободное и бесплатное, распространение — со ссылкой на источник.

Наружу уходят только координаты отеля и дата — ни слова гостя, ни его данных: передачей
ПД это не является (PII_REGISTRY не меняется).

**Р-3. Кэш и деградация — в адаптере.** Правила кэша — условия провайдера, поэтому живут в
адаптере. Погода: копия до `Expires`, затем запрос с `If-Modified-Since`; провайдер не ответил —
прежняя копия, пока она моложе 3 часов. Курс: установленный на саму дату окончателен — копия
бессрочна; курс предыдущего дня — 10 минут (свежий мог выйти). Неудачный запрос закрывает
провайдера на 60 с (простейший circuit breaker, P-8): гость на соседнем ходу не ждёт второго
таймаута. Таймаут вызова — 3 с: гость его ждёт. Нет данных — исключение порта; блок пишет
«данных нет», бот честно говорит, что сейчас не знает (FOUNDATION §8 правило 2).

**Р-4. Точка и город — конфиг тенанта, адаптер — окружение.** Где отель, знает его конфиг
(`TenantConfig`, NG-7); `WEATHER_BACKEND` (`fake` | `met_norway`) и `EXCHANGE_RATES_BACKEND`
(`fake` | `nbk`) — свойство контура, как у хранилища и антивируса. Сутки и «сегодня» — по поясу
отеля (FOUNDATION §9).

**Р-5. Курс — справка, не расчёт.** Бот называет официальный курс на дату с оговоркой: в
обменниках и на стойке курс свой. Суммы отеля, счёт гостя и цены каталога по нему не
пересчитываются — сколько платить, говорит ресепшен (NG-4 по духу: деньги — человеку).

## 3. Контракты (PR 1)

`shared/ports/weather.py` — `WeatherPort` по канону `ports/file_storage.py`:
- `forecast(point: GeoPoint) -> WeatherForecast` — почасовой прогноз от текущего часа: `hours` —
  `WeatherHour(starts_at UTC, air_temperature_c, wind_speed_ms, condition, precipitation_mm)`
  по возрастанию, хотя бы один; `updated_at` — когда провайдер посчитал прогноз;
- `condition` — `WeatherCondition` (12 значений: ясно … гроза) или `None`, если код провайдера
  незнаком; `GeoPoint` сам округляет координаты до 4 знаков и отвергает вне диапазона;
- нет прогноза — `WeatherUnavailableError`; `check()` — отвечает ли провайдер.

`shared/ports/exchange_rates.py` — `ExchangeRatesPort`:
- `official_rates(on: date) -> OfficialRates` — курсы, действующие в день `on`: на эту дату,
  иначе последние за 3 предыдущих дня; `effective_date` — дата курса, `rates` —
  `OfficialRate(currency, units, rate)`: `rate` тенге за `units` единиц, как опубликовано;
- нет курса — `ExchangeRatesUnavailableError`; `check()` — отвечает ли провайдер.

Адаптеры — `integrations/weather/` (`FakeWeatherAdapter`, `MetNorwayWeatherAdapter`) и
`integrations/exchange_rates/` (`FakeExchangeRatesAdapter`, `NbkExchangeRatesAdapter`), выбор —
`build_weather(settings)` / `build_exchange_rates(settings)`. Метрика
`concierge_source_calls_total{source, outcome}` — каждый запрос к провайдеру: `ok`,
`not_modified`, `error`. Коды — ERR-CONCIERGE-001 (погода), ERR-CONCIERGE-002 (курс),
[errors.md](../runbooks/errors.md).

## 4. Блок хода (PR 2)

Флаг `ENABLE_CONCIERGE_CITY` (умолчание `false`; staging — общим выключателем
`STAGING_ALL_FEATURES`, гостевая копия — только по слову основателя) и поле конфига тенанта
`concierge_city` (название, широта, долгота); нет поля — блока нет. Оркестратор ставит блок
среди блоков хода, после отметки кэша #138: он меняется каждый час. Текст (по-английски, как
весь промпт):

```
# Weather and exchange rates

Weather forecast for Almaty by MET Norway, hotel local time (updated 06:00):
- now: 3 °C, cloudy, wind 2 m/s
- Wednesday 11 November: 09:00 4 °C cloudy · 12:00 6 °C light rain · 15:00 6 °C light rain · …
- Thursday 12 November: 00:00 0 °C cloudy · …

Official exchange rates of the National Bank of Kazakhstan for 11 November 2026, in tenge (KZT):
1 USD = 447.52 · 1 EUR = 502.21 · 1 RUB = 5.41 · 1 CNY = 67.09 · 100 KRW = 33.44 · …

Use this only when the guest asks about the weather, what to wear, or about exchange rates.
Do not bring it up yourself. When you give the weather, say once, briefly, that it is a
MET Norway forecast. When you give a rate, say it is the official National Bank of Kazakhstan
rate for that date and that exchange offices and the hotel's front desk have their own rates.
Never convert the hotel's prices or the guest's bill with these rates: what the guest pays,
the front desk tells them.
```

Нет данных — вместо строк погоды или курса одна строка «… is not available right now: say so
honestly, do not guess». Прогноз — каждые 3 часа до конца завтрашнего дня по поясу отеля;
валюты — USD, EUR, RUB, CNY, GBP, KGS, UZS, TRY, KRW, JPY, AED, INR.

## 5. Тесты

- PR 1: один контрактный набор на Fake и настоящий адаптер каждого порта
  (`tests/test_weather_port.py`, `tests/test_exchange_rates_port.py`, R-7): настоящий говорит с
  `httpx.MockTransport`, который отвечает форматом провайдера из состояния Fake, — ответы обязаны
  совпасть; отдельно — `User-Agent`, 4 знака, `Expires` и `If-Modified-Since`, 304, 429,
  прежняя копия при отказе и её предел, пауза после отказа, ответ не по формату, курс на
  вчера при пустом сегодня.
- PR 2: текст блока при данных и без них, сутки по поясу отеля, флаг и конфиг; деградация —
  провайдер лежит, ход идёт (FOUNDATION §8 правило 2).
- Evals на 6 языках (§7.7) — до включения на гостевой копии, платно: план с ценой — диспетчеру
  (CLAUDE.md, «Платный прогон»). Мерить: блок молчит без вопроса гостя, называет источник,
  не пересчитывает цены отеля, язык ответа — гостя.
