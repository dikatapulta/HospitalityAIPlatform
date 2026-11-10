# staff_portal · «Служебные записки»

Часть паспорта кабинета персонала (R-4, #891): раздел «Служебные записки» ([spec 0077](../../../../docs/specs/0077-staff-memos.md), #936) и плашка о привязке устройства подписи на каждой странице отеля. Общее устройство, вход и безопасность — [README.md](../README.md). Правка раздела обновляет этот файл тем же PR.

Серия §11: PR 4б-1 (этот файл впервые) — «Моя подпись», плашка и «Сбросить подпись»; вкладки записок, окно подтверждения, карточка, печать и пуши — PR 4б-2. Данные и подтверждение — модуль `modules/memos` и `platform/staff_confirm`, `staff_passkey_binding`, `staff_passkey_notice`.

## Состав

| Файл | Что даёт | Задача |
| --- | --- | --- |
| `memos.py` | Раздел: флаг отеля `TenantConfig.memos_enabled` (`section_on` — меню, `require_section` — 404 у страниц и действий), права — `require_role` любого уровня, только своё; тексты сотруднику §9 и записи founder-review PR #987 (`MESSAGES` — ответы действий для JS); `bind_refusal` — почему этой сессии нельзя привязать устройство (суперадмин, вход без «Запомнить меня» — то же условие, что `staff_passkey_binding._require_remembered`); контекст «Моей подписи»: устройства по поясу отеля, образец (`strokes_points` — штрихи атрибутом `points`, только целые числа) | #936 |
| `memos_pages.py` | `GET …/memos` и `…/memos/{tab}` — вкладка одним маршрутом; без вкладки — первая | #936 |
| `memo_signature_api.py` | `POST …/api/memos/signature/bind/begin` (пароль → параметры `navigator.credentials.create`), `…/bind/finish` (ответ ключа → название устройства; его сервер берёт из `User-Agent` запроса, с клиента не приходит — аудит #949, В2-7; пуш владельцу ведёт на главную отеля), `…/devices/{id}/revoke` (своё, с паролем; чужой id — `revoked: false`), `…/sample` (образец: штрихи в поле 0…1000 × 0…400, пределы модуля — 422) | #936 |
| `passkey_notice_api.py` | Плашка о привязке: `notice_context` — для `pages.hotel_page` (непрочитанные привязки этой сессии, текст §9 по поясу отеля); `POST …/api/passkey-notice/{id}/ack` — «Это я», `…/{id}/disown` — «Это не я» с паролем (гасит ключ и все остальные входы учётки). Раздела записок не требует: выключенный раздел не отменяет уже привязанное чужое устройство. `ok: false` — закрывать или гасить нечего из этой сессии | #936 |
| `templates/memo_signature.html`, `_memo_tabs.html`, `_memo_signature_svg.html` | «Моя подпись»: устройство (форма пароля видна, только если браузер умеет встроенный ключ — иначе текст «подписывайте паролем»), список устройств с «Отвязать», образец рисунком SVG и поле для подписи; вкладки раздела; макрос рисунка образца — его же возьмёт лист подписей PR 4б-2 | #936 |
| `static/passkey.js` | `window.NecturnPasskey`: `available()`, `create(optionsJson)`, `get(optionsJson)` — перевод base64url ↔ байты руками (встроенного `parseCreationOptionsFromJSON` нет на части телефонов пилота); `get` — для окна подтверждения PR 4б-2 | #936 |
| `static/memo_signature.js` + `static/memos.css` | Привязка, отвязка, поле для подписи (пальцем или мышью; прореживание под 4000 точек модуля, не больше 64 штрихов); успех — перезагрузка, текст успеха переживает её через `sessionStorage`; тексты — `data-msg-*` | #936 |
| `static/passkey_notice.js` | Кнопки плашки в `layout.html`: «Это я», «Это не я» → поле пароля; ответы — текстом из `data-msg-*` | #936 |
| `team.py`, `team_api.py`, `static/team.js` (кнопка) | «Сбросить подпись» в «Сотрудниках» — менеджеру, при включённых записках, не себе и не суперадмину: `POST …/api/team/members/{user_id}/signature-reset` — `staff_team.reset_member_passkeys` (ключи во всех отелях учётки), затем образец в этом отеле (`memos_api.revoke_signature_sample`) | #936 |
| `shell.py` (пункт меню) | «Служебные записки» — `memos.section_on`, иконка `pen` | — |

## Маршруты

- `GET /staff/{tenant_slug}/memos` и `…/memos/signature` — «Моя подпись» (при `TenantConfig.memos_enabled`, иначе 404 у страниц и действий; любой уровень).
- `POST …/api/memos/signature/bind/begin|bind/finish|devices/{id}/revoke|sample` — JSON-действия «Моей подписи»; отказы — ERR-AUTH-017 (пароль), ERR-AUTH-006 (бюджет попыток входа), ERR-AUTH-018 (привязка: `not_remembered`, `platform_admin`, `limit`, `already_bound`, `invalid`, `expired`).
- `POST …/api/passkey-notice/{id}/ack|disown` — плашка о привязке (любой уровень, без флага раздела).
- `POST …/api/team/members/{user_id}/signature-reset` — «Сбросить подпись» (менеджер, при флаге раздела).

## Безопасность

- Привязка — только во входе с «Запомнить меня» и не суперадмину; страница говорит это заранее, сервер отказывает сам (`staff_passkey_binding`).
- Пароль во всех трёх местах (привязка, отвязка, «Это не я») — под бюджетами входа (`staff_passkeys.require_password`).
- Плашку видят и закрывают только другие входы владельца; привязавший вход её не видит (`staff_passkey_notice.unseen_bindings`).
- В логах — только id (события платформы `staff.passkey_*`, `memo_signature_sample_*`); пароля и названия устройства в логах маршрутов нет.
