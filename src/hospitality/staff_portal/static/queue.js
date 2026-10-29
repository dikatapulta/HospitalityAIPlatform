/* Очередь заявок кабинета (spec 0033 §5, PR D серии #48).
 *
 * Ванильный JS без сборки (ADR-014): поллинг списка каждые 15 с + JSON-действия
 * «взять» / «готово» / «отменить» / «вернуть в очередь» / «передать» (spec 0042),
 * у заказа в номер — «Принял» / «Оплачено» / «Доставлено» / «Отменить» с причиной
 * (spec 0038 §6 PR 6, `…/api/orders`). Отдельный файл, не inline-скрипт — CSP
 * страниц кабинета default-src 'self' (router.py). Разметка карточек живёт
 * только в Jinja (_queue_list.html): обновление — замена innerHTML контейнера
 * ответом fragment-эндпоинта, JS ничего не рисует сам.
 *
 * CSRF-контракт JSON-действий (докстринг router.py): каждый POST несёт
 * Content-Type: application/json (даже с пустым телом) — Origin браузер
 * добавляет сам, сервер требует оба заголовка.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 15000;

  var list = document.getElementById("queue-list");
  var status = document.getElementById("queue-status");
  if (!list || !status) return;
  var endpoint = list.dataset.endpoint;
  var ordersEndpoint = list.dataset.ordersEndpoint;
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
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию."
  };

  /* Строка над списком: ошибка — красным, итог передачи и возврата — зелёным
   * (карточка могла уйти из отфильтрованного списка, и без строки действие
   * выглядело бы пропажей). */
  function showStatus(text, ok) {
    status.textContent = text;
    status.className = ok ? "hint ok" : "error";
    status.hidden = !text;
  }

  function numberOf(data) {
    return data && data.daily_number !== null && data.daily_number !== undefined
      ? "Заявка #" + data.daily_number : "Заявка";
  }

  var refreshing = false;
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      var response = await fetch(fragmentUrl, { cache: "no-store" });
      if (response.redirected) {
        /* Сессия истекла: fragment ответил редиректом на логин — уходим туда же. */
        window.location.href = response.url;
        return;
      }
      if (response.ok) list.innerHTML = await response.text();
    } catch (error) {
      /* Нет сети — тик пропущен, следующий поллинг попробует снова. */
    } finally {
      refreshing = false;
    }
  }

  setInterval(function () {
    if (!document.hidden) refresh();
  }, POLL_INTERVAL_MS);

  /* `successText(data)` — строка итога по ответу (заявка после действия) или
   * null: «взять» и «готово» видны по самой карточке. `base` — адрес действий:
   * по умолчанию заявки, у «Оплачено» и отмены заказа — `ordersEndpoint`. */
  async function post(card, action, body, successText, base) {
    card.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
    showStatus("");
    try {
      var url = (base || endpoint) + "/" + card.dataset.requestId + "/" + action;
      var response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (!response.ok) {
        var code = "";
        try { code = (await response.json()).error.code; } catch (error) { /* не конверт */ }
        showStatus(MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.");
      } else if (successText) {
        var data = null;
        try { data = await response.json(); } catch (error) { /* итог без номера */ }
        showStatus(successText(data), true);
      }
    } catch (error) {
      showStatus("Нет связи — попробуйте ещё раз.");
    }
    /* И при успехе, и при конфликте показываем актуальное состояние очереди. */
    await refresh();
    /* Сигнал кабинета (signal.js) перечитывает невзятые сразу: повтор срочной
     * не должен звучать после «Взять» до следующего опроса. */
    document.dispatchEvent(new Event("staff-queue-changed"));
  }

  function hideForms(card) {
    card.querySelectorAll("form").forEach(function (form) { form.hidden = true; });
  }

  function openOrderCancelForm(card) {
    var form = card.querySelector("[data-order-cancel-form]");
    if (!form) return;
    hideForms(card);
    form.hidden = false;
    form.querySelector("select").focus();
  }

  function openTransferForm(card) {
    var form = card.querySelector("[data-transfer-form]");
    if (!form) return;
    hideForms(card);
    form.hidden = false;
    form.querySelector("select").focus();
  }

  function openNoteForm(card, action) {
    var form = card.querySelector("[data-note-form]");
    if (!form) return;
    hideForms(card);
    var label = form.querySelector("[data-note-label]");
    var input = form.querySelector("input[name=note]");
    form.dataset.pending = action;
    if (action === "cancel") {
      label.textContent = "Причина отмены (обязательно)";
    } else {
      label.textContent = "Примечание, если сделано не всё (необязательно)";
    }
    form.hidden = false;
    input.focus();
  }

  list.addEventListener("click", function (event) {
    var formCancel = event.target.closest("[data-form-cancel]");
    if (formCancel) {
      formCancel.closest("form").hidden = true;
      return;
    }
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var card = button.closest(".request-card");
    var action = button.dataset.action;
    if (action === "claim") {
      post(card, "claim", {}, null);
    } else if (action === "paid") {
      post(card, "paid", {}, null, ordersEndpoint);
    } else if (action === "deliver") {
      /* «Доставлено» — общий «Готово» без примечания (ревью PR #520, Н-9). */
      post(card, "complete", {}, null);
    } else if (action === "order-cancel") {
      openOrderCancelForm(card);
    } else if (action === "requeue") {
      /* Без формы: ошибку исправит любое «Взять» (spec 0042 §4). */
      post(card, "requeue", {}, function (data) {
        return numberOf(data) + " возвращена в очередь.";
      });
    } else if (action === "transfer") {
      openTransferForm(card);
    } else {
      openNoteForm(card, action);
    }
  });

  list.addEventListener("submit", function (event) {
    var orderCancelForm = event.target.closest("[data-order-cancel-form]");
    if (orderCancelForm) {
      event.preventDefault();
      post(orderCancelForm.closest(".request-card"), "cancel",
        { reason: orderCancelForm.querySelector("select").value }, null, ordersEndpoint);
      return;
    }
    var transferForm = event.target.closest("[data-transfer-form]");
    if (transferForm) {
      event.preventDefault();
      var select = transferForm.querySelector("select");
      var serviceName = select.options[select.selectedIndex].text;
      post(transferForm.closest(".request-card"), "transfer", { category_id: select.value },
        function (data) { return numberOf(data) + " передана: " + serviceName + "."; });
      return;
    }
    var form = event.target.closest("[data-note-form]");
    if (!form) return;
    event.preventDefault();
    var card = form.closest(".request-card");
    var note = form.querySelector("input[name=note]").value.trim();
    if (form.dataset.pending === "cancel" && !note) {
      showStatus("Укажите причину отмены.");
      return;
    }
    post(card, form.dataset.pending, note ? { note: note } : {}, null);
  });
})();
