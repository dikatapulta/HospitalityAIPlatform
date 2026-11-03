/* Очередь заявок кабинета (spec 0033 §5, PR D серии #48).
 *
 * Ванильный JS без сборки (ADR-014): поллинг списка каждые 15 с + JSON-действия
 * «взять» / «готово» / «отменить» / «вернуть в очередь» / «передать» (spec 0042),
 * у заказа в номер — «Принял» / «Оплачено» / «Доставлено» / «Отменить» с причиной
 * (spec 0038 §6 PR 6, `…/api/orders`), у запроса столика — три ответа ресторана и
 * «Закрыть запрос» (spec 0064 §2.3, `…/api/bookings`). Отдельный файл, не inline-скрипт — CSP
 * страниц кабинета default-src 'self' (router.py). Разметка карточек живёт
 * только в Jinja (_queue_list.html): обновление — замена innerHTML контейнера
 * ответом fragment-эндпоинта, JS ничего не рисует сам.
 *
 * CSRF-контракт JSON-действий (докстринг router.py): каждый POST несёт
 * Content-Type: application/json (даже с пустым телом) — Origin браузер
 * добавляет сам, сервер требует оба заголовка.
 *
 * Кабинет v3 (#636): карточка открывается панелью #request-panel (окно каркаса,
 * app_windows.js) — в неё кладётся копия карточки из списка, и те же обработчики
 * ведут её кнопки; после обновления списка копия заменяется свежей, пропала
 * заявка из списка — панель закрывается. Итог действия — всплывашка; у «Взять»
 * в ней «Отменить» — «Вернуть в очередь», обратное действие сервера (spec 0042).
 * У остальных действий обратного нет — и кнопки нет. Ошибка — красной строкой
 * над списком или в панели, если действие было там.
 *
 * Кабинет v3, PR 5 (#638): заявка в списке — строка с одним главным действием
 * («Взять себе», «Готово», шаг заказа); «Готово» строки открывает панель с полем
 * примечания — формам в строке места нет.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 15000;

  var list = document.getElementById("queue-list");
  var status = document.getElementById("queue-status");
  if (!list || !status) return;
  var panel = document.getElementById("request-panel");
  var panelBody = panel ? panel.querySelector("[data-panel-body]") : null;
  var panelStatus = panel ? panel.querySelector("[data-panel-status]") : null;
  /* Id заявки в открытой панели; null — панель закрыта. */
  var panelId = null;
  var endpoint = list.dataset.endpoint;
  var ordersEndpoint = list.dataset.ordersEndpoint;
  var bookingsEndpoint = list.dataset.bookingsEndpoint;
  var fragmentUrl = list.dataset.fragmentUrl;

  /* Дружелюбные тексты по кодам каталога ошибок (R-8): «уже взята» — spec 0033 §8. */
  var MESSAGES = {
    "ERR-REQUESTS-003": "Заявка уже взята или закрыта — список обновлён.",
    "ERR-REQUESTS-005": "Заявку уже закрыли или передали — список обновлён.",
    "ERR-REQUESTS-007": "Эта заявка уже в другой службе — список обновлён.",
    "ERR-REQUESTS-001": "Такой службы больше нет — список обновлён.",
    "ERR-REQUESTS-002": "Заявка не найдена — список обновлён.",
    /* Заказ в номер (spec 0038 §4). */
    "ERR-REQUESTS-006": "Сначала отметьте оплату — кнопка «Оплачено».",
    "ERR-REQUESTS-008": "Оплату уже отметили или заказ закрыт — список обновлён.",
    "ERR-ROOMSERVICE-008": "Заказ не найден — список обновлён.",
    "ERR-ROOMSERVICE-009": "Оплата уже отмечена — выберите другую причину.",
    /* Запрос столика (spec 0064 §4). */
    "ERR-TABLEBOOKING-002": "Это время уже не предложить — выберите другое.",
    "ERR-TABLEBOOKING-004": "Бронь не найдена — список обновлён.",
    "ERR-TABLEBOOKING-005": "На этот запрос уже ответили или гость его отменил — список обновлён.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию."
  };

  /* Ошибка — красной строкой там, где было действие: над списком или в панели. */
  function showStatus(text, card) {
    var line = card && panel && panel.contains(card) ? panelStatus : status;
    status.hidden = true;
    if (panelStatus) panelStatus.hidden = true;
    line.textContent = text;
    line.hidden = !text;
  }

  /* Итог действия — всплывашкой (карточка могла уйти из отфильтрованного
   * списка, и без неё действие выглядело бы пропажей); `undo` — обратное
   * действие сервера или null. */
  function toast(text, undo) {
    if (window.StaffWindows) window.StaffWindows.toast(text, undo || null);
  }

  function numberOf(data) {
    return data && data.daily_number !== null && data.daily_number !== undefined
      ? "Заявка #" + data.daily_number : "Заявка";
  }

  /* Запрос столика: номер в ответе — `number` брони, а не `daily_number` заявки. */
  function bookingNumber(data) {
    return data && data.number !== null && data.number !== undefined ? " #" + data.number : "";
  }

  /* Открытая форма (примечание, передача, отмена заказа) — сотрудник пишет:
   * тик поллинга список не заменяет, иначе недописанный текст пропал бы (#221).
   * После своего действия список обновляется принудительно (`force`). */
  function formIsOpen() {
    var inPanel = panelBody !== null && panelBody.querySelector("form:not([hidden])") !== null;
    return inPanel || list.querySelector("form:not([hidden])") !== null;
  }

  function cardInList(id) {
    return list.querySelector('.request-card[data-request-id="' + CSS.escape(id) + '"]');
  }

  /* Копия карточки в панель: id полей — с префиксом, чтобы подписи форм вели
   * к полям панели, а не к спрятанным в списке. */
  function fillPanel(card) {
    var copy = card.cloneNode(true);
    copy.querySelectorAll("[id]").forEach(function (node) { node.id = "panel-" + node.id; });
    copy.querySelectorAll("label[for]").forEach(function (node) {
      node.htmlFor = "panel-" + node.htmlFor;
    });
    panel.querySelector("[data-panel-title]").textContent = card.dataset.panelTitle || "";
    panel.querySelector("[data-panel-sub]").textContent = card.dataset.panelSub || "";
    panelBody.replaceChildren(copy);
  }

  function openPanel(card, opener) {
    if (!panel || !window.StaffWindows) return;
    panelId = card.dataset.requestId;
    showStatus("");
    fillPanel(card);
    window.StaffWindows.open("request-panel", opener);
  }

  /* После обновления списка: свежая копия в панель или закрыть, если заявки
   * в этом списке больше нет (закрыли, передали, ушла из фильтра). */
  function syncPanel() {
    if (panelId === null) return;
    var fresh = cardInList(panelId);
    if (!fresh) {
      window.StaffWindows.close();
      return;
    }
    var focusWasInside = panel.contains(document.activeElement);
    fillPanel(fresh);
    if (focusWasInside && !panel.contains(document.activeElement)) {
      panel.querySelector("[role=dialog]").focus();
    }
  }

  if (panel) {
    panel.addEventListener("staff-window-close", function (event) {
      var card = panelId === null ? null : cardInList(panelId);
      panelId = null;
      panelBody.replaceChildren();
      /* Список мог обновиться, пока панель была открыта, — кнопка, которой её
       * открыли, уже заменена: фокус — на номер той же карточки в свежем списке. */
      var number = card && card.querySelector("[data-request-open]");
      if (number && !event.detail.silent) number.focus();
    });
  }

  var refreshing = false;
  async function refresh(force) {
    if (refreshing || (!force && formIsOpen())) return;
    refreshing = true;
    try {
      var response = await fetch(fragmentUrl, { cache: "no-store" });
      if (response.redirected) {
        /* Сессия истекла: fragment ответил редиректом на логин — уходим туда же. */
        window.location.href = response.url;
        return;
      }
      var html = response.ok ? await response.text() : null;
      /* Форму могли открыть, пока ответ шёл по сети, — проверка ещё раз. */
      if (html !== null && (force || !formIsOpen())) {
        list.innerHTML = html;
        syncPanel();
      }
    } catch (error) {
      /* Нет сети — тик пропущен, следующий поллинг попробует снова. */
    } finally {
      refreshing = false;
    }
  }

  setInterval(function () {
    if (!document.hidden) refresh();
  }, POLL_INTERVAL_MS);

  function failureText(code, httpStatus) {
    return MESSAGES[code] || "Не получилось (" + (code || httpStatus) + "). Попробуйте ещё раз.";
  }

  /* `successText(data)` — строка итога по ответу (заявка после действия) или
   * null: у заказа шаги видны по самой карточке. `base` — адрес действий:
   * по умолчанию заявки, у «Оплачено» и отмены заказа — `ordersEndpoint`.
   * `undo(id)` — обратное действие для «Отменить» во всплывашке или null. */
  async function post(card, action, body, successText, base, undo) {
    var id = card.dataset.requestId;
    card.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
    showStatus("");
    try {
      var url = (base || endpoint) + "/" + id + "/" + action;
      var response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (!response.ok) {
        var code = "";
        try { code = (await response.json()).error.code; } catch (error) { /* не конверт */ }
        showStatus(failureText(code, response.status), card);
      } else if (successText) {
        var data = null;
        try { data = await response.json(); } catch (error) { /* итог без номера */ }
        toast(successText(data), undo ? function () { undo(id); } : null);
      }
    } catch (error) {
      showStatus("Нет связи — попробуйте ещё раз.", card);
    }
    /* И при успехе, и при конфликте показываем актуальное состояние очереди. */
    await refresh(true);
    /* Сигнал кабинета (signal.js) перечитывает невзятые сразу: повтор срочной
     * не должен звучать после «Взять» до следующего опроса. */
    document.dispatchEvent(new Event("staff-queue-changed"));
  }

  /* «Отменить» после «Взять»: заявка возвращается в очередь (spec 0042 §2.1). */
  async function requeue(id) {
    try {
      var response = await fetch(endpoint + "/" + id + "/requeue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не конверт */ }
      if (response.ok) {
        toast(numberOf(data) + " возвращена в очередь", null);
      } else {
        toast(failureText(data && data.error && data.error.code, response.status), null);
      }
    } catch (error) {
      toast("Нет связи — попробуйте ещё раз.", null);
    }
    await refresh(true);
    document.dispatchEvent(new Event("staff-queue-changed"));
  }

  function hideForms(card) {
    card.querySelectorAll("form").forEach(function (form) { form.hidden = true; });
  }

  /* Форма со списком: отмена заказа, «Отменить» (spec 0057 §2: поле текста —
   * только у «Другое»), «Передать», «Другое время» у запроса столика. */
  function openSelectForm(card, selector) {
    var form = card.querySelector(selector);
    if (!form) return;
    hideForms(card);
    form.hidden = false;
    form.querySelector("select").focus();
  }

  function toggleCancelNote(form) {
    var other = form.querySelector("select").value === "other";
    var note = form.querySelector("[data-cancel-note]");
    note.hidden = !other;
    note.querySelector("input").required = other;
    if (other) note.querySelector("input").focus();
  }

  function openNoteForm(card, action) {
    var form = card.querySelector("[data-note-form]");
    if (!form) return;
    hideForms(card);
    var label = form.querySelector("[data-note-label]");
    var input = form.querySelector("input[name=note]");
    form.dataset.pending = action;
    label.textContent = "Примечание, если сделано не всё (необязательно)";
    form.hidden = false;
    input.focus();
  }

  /* Клик по самой карточке в списке (не по кнопке, ссылке или полю) — панель. */
  var INTERACTIVE = "button, a, input, select, textarea, label, form";
  /* Действия, которые сначала открывают форму, а не шлют запрос сразу. */
  var FORM_ACTIONS = ["complete", "transfer", "cancel", "order-cancel"];

  function onClick(event) {
    var formCancel = event.target.closest("[data-form-cancel]");
    if (formCancel) {
      formCancel.closest("form").hidden = true;
      return;
    }
    var opener = event.target.closest("[data-request-open]");
    var listCard = list.contains(event.target) ? event.target.closest(".request-card") : null;
    if (listCard && (opener || !event.target.closest(INTERACTIVE))) {
      /* Выделяли текст карточки мышью — это не клик «открыть». */
      /* Фокус после закрытия — на номер карточки, даже если открыли кликом. */
      if (opener || String(window.getSelection()) === "") {
        openPanel(listCard, opener || listCard.querySelector("[data-request-open]"));
      }
      return;
    }
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var card = button.closest(".request-card");
    var action = button.dataset.action;
    /* Строка списка (#638) несёт одно действие; если ему нужна форма («Готово»
     * с примечанием), форма открывается в панели — в строке её негде показать. */
    if (FORM_ACTIONS.indexOf(action) !== -1 && list.contains(card) && panel && window.StaffWindows) {
      openPanel(card, card.querySelector("[data-request-open]"));
      card = panelBody.querySelector(".request-card");
    }
    if (action === "claim") {
      /* У заказа «Иду за оплатой» — тоже claim, но «Вернуть в очередь» у него нет. */
      var isOrder = card.hasAttribute("data-order");
      post(card, "claim", {}, isOrder ? null : function (data) {
        return numberOf(data) + " теперь ваша";
      }, null, isOrder ? null : requeue);
    } else if (action === "paid") {
      post(card, "paid", {}, null, ordersEndpoint);
    } else if (action === "deliver") {
      /* «Доставлено» — общий «Готово» без примечания (ревью PR #520, Н-9). */
      post(card, "complete", {}, null);
    } else if (action === "order-cancel") {
      openSelectForm(card, "[data-order-cancel-form]");
    } else if (action === "booking-confirm" || action === "booking-no-seats") {
      answerBooking(card, { answer: action === "booking-confirm" ? "confirmed" : "no_seats" });
    } else if (action === "booking-other-time") {
      openSelectForm(card, "[data-booking-time-form]");
    } else if (action === "booking-close") {
      post(card, "close", {}, function (data) {
        return "Запрос на столик" + bookingNumber(data) + " закрыт";
      }, bookingsEndpoint);
    } else if (action === "requeue") {
      /* Без формы: ошибку исправит любое «Взять» (spec 0042 §4). */
      post(card, "requeue", {}, function (data) {
        return numberOf(data) + " возвращена в очередь";
      });
    } else if (action === "transfer") {
      openSelectForm(card, "[data-transfer-form]");
    } else if (action === "cancel") {
      openSelectForm(card, "[data-cancel-form]");
    } else {
      openNoteForm(card, action);
    }
  }

  /* Ответ ресторана закрывает заявку — карточка уходит из открытых, итог — всплывашкой. */
  function answerBooking(card, body) {
    post(card, "answer", body, function (data) {
      return "Ответ ушёл гостю — столик" + bookingNumber(data);
    }, bookingsEndpoint);
  }

  function onChange(event) {
    var cancelForm = event.target.closest("[data-cancel-form]");
    if (cancelForm && event.target.name === "reason") toggleCancelNote(cancelForm);
  }

  function onSubmit(event) {
    var orderCancelForm = event.target.closest("[data-order-cancel-form]");
    if (orderCancelForm) {
      event.preventDefault();
      post(orderCancelForm.closest(".request-card"), "cancel",
        { reason: orderCancelForm.querySelector("select").value }, null, ordersEndpoint);
      return;
    }
    var bookingTimeForm = event.target.closest("[data-booking-time-form]");
    if (bookingTimeForm) {
      event.preventDefault();
      answerBooking(bookingTimeForm.closest(".request-card"),
        { answer: "other_time", proposed_time: bookingTimeForm.querySelector("select").value });
      return;
    }
    var cancelForm = event.target.closest("[data-cancel-form]");
    if (cancelForm) {
      event.preventDefault();
      var reason = cancelForm.querySelector("select").value;
      var cancelNote = cancelForm.querySelector("input[name=note]").value.trim();
      if (!reason) {
        showStatus("Выберите причину отмены.", cancelForm);
        return;
      }
      if (reason === "other" && !cancelNote) {
        showStatus("Напишите причину — гость прочитает её в чате.", cancelForm);
        return;
      }
      post(cancelForm.closest(".request-card"), "cancel",
        reason === "other" ? { reason: reason, note: cancelNote } : { reason: reason },
        function (data) { return numberOf(data) + " отменена"; });
      return;
    }
    var transferForm = event.target.closest("[data-transfer-form]");
    if (transferForm) {
      event.preventDefault();
      var select = transferForm.querySelector("select");
      var serviceName = select.options[select.selectedIndex].text;
      post(transferForm.closest(".request-card"), "transfer", { category_id: select.value },
        function (data) { return numberOf(data) + " передана: " + serviceName; });
      return;
    }
    var form = event.target.closest("[data-note-form]");
    if (!form) return;
    event.preventDefault();
    var card = form.closest(".request-card");
    var note = form.querySelector("input[name=note]").value.trim();
    post(card, form.dataset.pending, note ? { note: note } : {}, function (data) {
      return numberOf(data) + " выполнена";
    });
  }

  [list, panelBody].forEach(function (root) {
    if (!root) return;
    root.addEventListener("click", onClick);
    root.addEventListener("change", onChange);
    root.addEventListener("submit", onSubmit);
  });

  /* Подсказка клавиш поиска (#638): на Mac — ⌘K, иначе остаётся Ctrl K из шаблона. */
  var searchKbd = document.querySelector("[data-kbd-search]");
  if (searchKbd && /Mac|iPhone|iPad/.test(navigator.platform || "")) searchKbd.textContent = "⌘K";

  /* Заявку создали окном каркаса (new_request.js) — показать её сразу. */
  document.addEventListener("staff-request-created", function () { refresh(true); });
})();
