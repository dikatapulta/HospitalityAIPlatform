# Публичный сайт necturn.com: выкладка, проверка, откат

Страница — [`site/`](../../site/README.md), решение о хостинге —
[ADR-019](../adr/ADR-019-public-site-static-pages.md). Сайт живёт отдельно от
приложения: его выкладывает Cloudflare Pages прямо из `main`, `deploy.sh` и сервер
staging тут ни при чём. Первичная настройка (§1–§2) — один раз и руками основателя:
доступ к Cloudflare есть только у него. Всё остальное — рутина или проверка, которую
делает сессия.

## 1. Pages-проект (один раз, ~10 минут)

Pages — это бесплатный хостинг Cloudflare для статических сайтов: он сам забирает
файлы из GitHub и раздаёт их с серверов Cloudflare.

1. Cloudflare → **Workers & Pages** → **Create application** → вкладка **Pages** →
   **Connect to Git**.
2. Войти в GitHub. На вопросе о доступе выбрать **Only select repositories** и отметить
   только `dikatapulta/HospitalityAIPlatform`: приложение Cloudflare не должно видеть
   чужие репозитории.
3. Выбрать репозиторий → **Begin setup** и заполнить:

   | Поле | Значение | Зачем |
   | --- | --- | --- |
   | Project name | `necturn-site` | Даст служебный адрес `necturn-site.pages.dev` |
   | Production branch | `main` | Сайт = то, что смержено |
   | Framework preset | `None` | Фреймворка нет |
   | Build command | `exit 0` | Сборки нет — команда «ничего не делать» |
   | Build output directory | `site` | Папка с `index.html` |
   | Root directory | *(пусто)* | — |
   | Environment variables → `SKIP_DEPENDENCY_INSTALL` | `1` | Иначе сборщик увидит `pyproject.toml` и начнёт ставить Python-зависимости приложения |

4. **Save and Deploy** → дождаться «Success» → открыть `https://necturn-site.pages.dev`:
   должна открыться страница.
5. Проект → **Settings** → **Build**:
   - **Build watch paths** → Include: `site/*`. Без этого Pages пересобирает сайт на
     каждый коммит в репозитории, даже если сайт не менялся.
   - **Branch control** → Preview branch: **None**. Черновики из PR не получают
     публичных адресов, а в PR не пишет лишний бот.

## 2. Домен necturn.com (один раз, ~5 минут + ожидание)

1. Проект → **Custom domains** → **Set up a custom domain** → `necturn.com` →
   **Continue** → **Activate domain**. Запись DNS Pages создаёт сам.
   **Не создавай A- или CNAME-запись для корня руками заранее** — Cloudflare
   предупреждает, что такой домен отдаёт ошибку 522.
2. Дождаться статуса **Active** (обычно минуты, редко до часа — выпускается сертификат).
3. Почтовые MX- и TXT-записи на корне домена при этом остаются на месте: корневая
   CNAME-запись Pages отдаётся как обычный адрес, и Cloudflare держит её рядом с
   почтовыми. Если панель при добавлении домена всё же сообщит о конфликте записей —
   остановиться и позвать сессию, ничего не удалять.
4. *(По желанию)* `www.necturn.com`: добавить вторым custom domain, затем
   **Rules** → **Redirect Rules** → шаблон **Redirect from WWW to root**.

## 3. Проверка после выкладки (сессия, 2 минуты)

Подвал страницы обещает «нет cookies, трекеров и аналитики». Страница сама ничего такого
не делает, но Cloudflare умеет дописывать в ответ свои скрипты и cookie настройками
зоны — проверяем, что обещание правдиво:

```bash
curl -sI https://necturn.com | grep -i -E '^(HTTP|content-security-policy|set-cookie)'
curl -s https://necturn.com | grep -c 'cdn-cgi'
```

Ожидается: `HTTP/2 200`, строка `content-security-policy`, **ни одной** `set-cookie`, и
`0` во второй команде.

- Есть `set-cookie: __cf_bm…` — включён **Security → Bots → Bot Fight Mode**. Либо
  выключить его для зоны, либо снять со страницы фразу про cookies: решает основатель.
- Вторая команда больше нуля — Cloudflare что-то встроил в страницу: **Scrape Shield →
  Email Address Obfuscation**, **Speed → Rocket Loader** или **Web Analytics** проекта.
  Выключить; CSP страницы такие скрипты всё равно блокирует, так что они только ломают.
- Открыть с телефона `https://necturn.com/?lang=kk` и `?lang=en`; в блоке «Контакт» нажать
  почту, телефон и WhatsApp — должны открыться почтовое приложение, звонилка и WhatsApp.

## 4. Почта на странице

Адрес `support@necturn.com` стоит в блоке «Контакт» внутри обёртки `<!--email_off-->`:
по ней Cloudflare не подменяет адрес, хотя подмена (**Scrape Shield → Email Address
Obfuscation**) у зоны включена по умолчанию. Без обёртки Cloudflare заменил бы адрес на
`[email protected]` и подгрузил расшифровывающий скрипт, который CSP страницы
заблокирует. Если §3 всё же показал `cdn-cgi` — выключить подмену для зоны.

## 5. Обновление и откат

- **Обновить сайт** — смержить PR, меняющий `site/`: Pages выложит его сам примерно
  за минуту. Посмотреть — проект → **Deployments**.
- **Откатить** — проект → **Deployments** → нужная прошлая выкладка → **⋯** →
  **Rollback to this deployment**. Откат мгновенный и не трогает `main`: исправление
  всё равно идёт следующим PR.
