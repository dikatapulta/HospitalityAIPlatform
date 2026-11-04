# Политика конфиденциальности 3D-витрины отеля (en/ru/kk)

> Spec 0069 §2.6 (issue #805, PR (в) серии #799). **Этот файл и есть публикуемая
> страница**: приложение отдаёт его по `GET /legal/showroom-privacy`
> (`platform/legal.py`), на этот адрес ссылается строка согласия под формами витрины
> (`channels/showroom/consent.py`). Правка файла = правка опубликованной политики.
>
> **Публикуется всё, что НИЖЕ первой строки `---`.** Этот блок — служебная преамбула,
> посетителю она не показывается.
>
> Статус: **вариант окна**, не проверен юристом. Отдельная страница, а не раздел
> политики гостевого чата: та относится к чату и проживанию, а посетитель витрины —
> не гость (агент группы или будущий гость). Строка согласия — «да» основателя 04.11
> для staging и показа 05.11 как есть (тело #805); **перед живыми агентами и гостями —
> юрист вместе с этой политикой и допсоглашение с отелем** (spec 0069 §2.6: контактов
> посетителей и срока 365 дней в пилотном договоре нет). Срок 365 дней — решение окна
> (spec 0069 §3, founder-review). Порядок языков — en первым: агенты из-за рубежа.
> Версия **1.0 (2026-11-04)**; меняешь текст по смыслу — подними
> `SHOWROOM_CONSENT_VERSION` в `channels/showroom/consent.py`.
>
> Строка согласия под обеими формами — дословно (копия в коде, сверяет
> `tests/test_legal.py`; в квадратных скобках — ссылка на эту страницу):
>
> - en: `By sending this request, I agree to the hotel and the platform processing the data I entered in order to answer this request — [showroom privacy policy].`
> - ru: `Отправляя запрос, я соглашаюсь на обработку указанных данных отелем и платформой для ответа на этот запрос — [политика конфиденциальности витрины].`
> - kk: `Сұрауды жіберу арқылы мен көрсетілген деректерді қонақүй мен платформаның осы сұрауға жауап беру үшін өңдеуіне келісемін — [витринаның құпиялылық саясаты].`

---

## English

### 3D Showroom Privacy Policy

Version 1.0, 04.11.2026. This policy covers the hotel's 3D showroom page: the building
model, the assistant and the request forms for groups and guests.

**1. Who processes the data.** The owner of the database is the hotel whose showroom
you are viewing. The operator (processing on the hotel's behalf) is IE «Alacrity»,
Kazakhstan, contact: privacy@necturn.com — the "platform" below.

**2. What data is processed.** Only what you enter in a request form: company (for
groups), contact person or name, phone and/or email, number of guests, dates, the rooms
you chose on the model or the view and room type you would like, your wishes, and the
page language. Payment card numbers are not requested; card numbers typed into wishes
are masked automatically. The page sets no analytics or advertising cookies; the only
cookie is the access code to the page, if the hotel has set one. Technical logs of the
service do not contain your contacts.

**3. Purpose.** To pass your request to the hotel's sales or reservations team and let
them answer you by phone or email. This is a request, not a booking: availability,
price and the booking itself are confirmed by a hotel employee.

**4. Basis — your consent**, which you give by pressing "Send request" under the line
that links to this policy.

**5. The assistant.** The assistant moves the camera around the model and can prepare a
request from your words; it never sends a request — only your button does. Phrases you
type to the assistant are sent to Anthropic (USA) to prepare the answer and are not
stored by the platform. Please do not type names or contacts to the assistant. Your
form data is not sent to the assistant.

**6. Who receives the data.** Hotel employees of the sales or reservations team,
reception and management. Also involved: Cloudflare (secure delivery of traffic), the
browser notification services of Google, Apple, Mozilla and Microsoft (USA; they deliver
an encrypted notice "Group request #12" to a hotel employee's phone, without your data),
the hosting provider, the backup storage (encrypted only), Sentry (technical error
reports without your data), Zoho (USA; the operator's mail service — if you email the
operator yourself).

**7. Retention.** Contacts, company and wishes — 365 days from the request, then they are
anonymized; the text of the hotel's task about your request is anonymized after 90 days.
Encrypted backups are kept up to 14 days and are renewed automatically.

**8. Your rights.** Access and copy, correction, blocking, deletion, withdrawal of
consent. Write to the operator at privacy@necturn.com or contact the hotel. Reply — within
3 working days.

**9. Changes.** A new version is published on this page.

---

## Русский

### Политика конфиденциальности 3D-витрины отеля

Версия 1.0, 04.11.2026. Политика относится к странице 3D-витрины отеля: модели здания,
помощнику и формам запроса для групп и гостей.

**1. Кто обрабатывает данные.** Собственник базы данных — отель, чью витрину вы
смотрите. Оператор (обработчик по поручению отеля) — ИП «Alacrity», Казахстан, контакт:
privacy@necturn.com. Далее — «платформа».

**2. Какие данные обрабатываются.** Только то, что вы вписали в форму запроса:
компания (для групп), контактное лицо или имя, телефон и/или почта, число гостей, даты,
номера, выбранные на модели, или желаемые вид и тип номера, пожелания и язык страницы.
Платёжные данные карт мы не запрашиваем; номер карты, вписанный в пожелания,
маскируется автоматически. Cookie аналитики и рекламы страница не ставит; единственная
cookie — код доступа к странице, если отель его задал. Технические журналы сервиса
ваших контактов не содержат.

**3. Цель.** Передать ваш запрос отделу продаж или бронирования отеля, чтобы вам
ответили по телефону или почте. Это запрос, а не бронь: наличие, цену и бронь
подтверждает сотрудник отеля.

**4. Основание — ваше согласие**, которое вы даёте кнопкой «Отправить запрос» под
строкой со ссылкой на эту политику.

**5. Помощник.** Помощник ведёт камеру по модели и может подготовить запрос по вашим
словам; отправить его он не может — только ваша кнопка. Фразы, которые вы пишете
помощнику, передаются сервису Anthropic (США) для ответа и платформой не хранятся.
Не пишите помощнику имена и контакты. Данные формы помощнику не передаются.

**6. Кому передаются данные.** Сотрудникам отдела продаж или бронирования отеля,
ресепшену и руководству. В обработке также участвуют: Cloudflare (защищённая доставка
трафика), службы уведомлений браузеров Google, Apple, Mozilla и Microsoft (США;
доставляют на телефон сотрудника отеля зашифрованное уведомление «Групповой запрос
#12», без ваших данных), хостинг-провайдер, хранилище резервных копий (только в
зашифрованном виде), Sentry (технические сообщения об ошибках, без ваших данных), Zoho
(США; почтовый сервис оператора — если вы сами пишете оператору на почту).

**7. Сроки хранения.** Контакты, компания и пожелания — 365 дней с запроса, затем
обезличиваются; текст задачи отеля по вашему запросу обезличивается через 90 дней.
Зашифрованные резервные копии хранятся до 14 дней и обновляются автоматически.

**8. Ваши права.** Доступ к данным и копия, исправление, блокирование, удаление, отзыв
согласия. Напишите оператору — privacy@necturn.com — или обратитесь в отель. Ответ — в
течение 3 рабочих дней.

**9. Изменения политики.** Новая версия публикуется на этой странице.

---

## Қазақша

### Қонақүйдің 3D-витринасының құпиялылық саясаты

Нұсқа 1.0, 04.11.2026. Саясат қонақүйдің 3D-витринасы бетіне қатысты: ғимарат
моделіне, көмекшіге және топтар мен қонақтарға арналған сұрау нысандарына.

**1. Деректерді кім өңдейді.** Деректер базасының меншік иесі — сіз витринасын қарап
отырған қонақүй. Оператор (қонақүйдің тапсырмасы бойынша өңдеуші) — ЖК «Alacrity»,
Қазақстан, байланыс: privacy@necturn.com. Бұдан әрі — «платформа».

**2. Қандай деректер өңделеді.** Тек сұрау нысанына өзіңіз енгізген деректер: компания
(топтар үшін), байланыс тұлғасы немесе есіміңіз, телефон және/немесе пошта, қонақтар
саны, күндер, модельде таңдалған нөмірлер немесе қалаған көрініс пен нөмір түрі,
тілектер және бет тілі. Банк картасының деректерін біз сұрамаймыз; тілектерге жазылған
карта нөмірі автоматты түрде бүркемеленеді. Бет талдау және жарнама cookie-файлдарын
орнатпайды; жалғыз cookie — қонақүй орнатса, бетке кіру коды. Сервистің техникалық
журналдарында байланыс деректеріңіз жоқ.

**3. Мақсат.** Сұрауыңызды қонақүйдің сату немесе брондау бөліміне жеткізу, сізге
телефон немесе пошта арқылы жауап беру үшін. Бұл брондау емес, сұрау: бос нөмірді,
бағаны және брондауды қонақүй қызметкері растайды.

**4. Негіз — сіздің келісіміңіз**, оны осы саясатқа сілтемесі бар жолдың астындағы
«Сұрауды жіберу» батырмасымен бересіз.

**5. Көмекші.** Көмекші камераны модель бойынша жүргізеді және сіздің сөздеріңіз
бойынша сұрауды дайындай алады; оны жібере алмайды — тек сіздің батырмаңыз. Көмекшіге
жазған сөйлемдеріңіз жауап дайындау үшін Anthropic сервисіне (АҚШ) беріледі және
платформада сақталмайды. Көмекшіге есімдер мен байланыс деректерін жазбаңыз. Нысан
деректері көмекшіге берілмейді.

**6. Деректер кімге беріледі.** Қонақүйдің сату немесе брондау бөлімінің
қызметкерлеріне, ресепшенге және басшылыққа. Өңдеуге сондай-ақ қатысады: Cloudflare
(трафикті қорғалған түрде жеткізу), Google, Apple, Mozilla және Microsoft браузерлерінің
хабарландыру қызметтері (АҚШ; қонақүй қызметкерінің телефонына «Топтық сұрау #12»
шифрланған хабарландыруын сіздің деректеріңізсіз жеткізеді), хостинг-провайдер,
резервтік көшірмелер қоймасы (тек шифрланған түрде), Sentry (сіздің деректеріңізсіз
қателер туралы техникалық хабарлар), Zoho (АҚШ; оператордың пошта сервисі — операторға
поштамен өзіңіз жазсаңыз).

**7. Сақтау мерзімдері.** Байланыс деректері, компания және тілектер — сұраудан бастап
365 күн, кейін иесіздендіріледі; сіздің сұрауыңыз бойынша қонақүй тапсырмасының мәтіні
90 күннен кейін иесіздендіріледі. Шифрланған резервтік көшірмелер 14 күнге дейін
сақталып, автоматты жаңарады.

**8. Сіздің құқықтарыңыз.** Деректерге қол жеткізу және көшірме алу, түзету, бұғаттау,
жою, келісімді кері қайтарып алу. Операторға жазыңыз — privacy@necturn.com — немесе
қонақүйге хабарласыңыз. Жауап — 3 жұмыс күні ішінде.

**9. Саясаттың өзгеруі.** Жаңа нұсқа осы бетте жарияланады.
