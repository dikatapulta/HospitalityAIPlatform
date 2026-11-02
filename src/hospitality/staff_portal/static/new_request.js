/* Форма «Новая заявка» кабинета (spec 0035 §5, PR C серии #299; окно — #636).
 *
 * Ванильный JS без сборки (ADR-014, канон queue.js). Одна форма в двух местах
 * (_new_request_form.html): окном поверх любой страницы отеля (data-mode="dialog")
 * и страницей для прямых ссылок (data-mode="page"). Поля окна JS берёт при каждом
 * открытии фрагментом data-fields-url — готовой разметкой сервера, как список
 * очереди (канон queue.js); заодно окно открывается чистым. Проверка трёх полей —
 * текстами §9, каждая ошибка под своим полем; один POST по CSRF-контракту
 * (Content-Type: application/json + Origin от fetch). «Сразу взять себе» —
 * второй POST, существующий …/requests/{id}/claim с теми же правами.
 * Успех: окно закрывается со всплывашкой (window.StaffWindows, app_windows.js)
 * и сообщает очереди staff-request-created; страница уходит в очередь с плашкой.
 */
(function () {
  "use strict";

  /* Дружелюбные тексты по кодам каталога ошибок (R-8), канон queue.js. */
  var MESSAGES = {
    "ERR-REQUESTS-001": "Такой службы у отеля нет — обновите страницу.",
    "ERR-PLATFORM-002": "Проверьте поля — заявку не приняли.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию."
  };
  /* Три текста отказа — §9 спеки. */
  var FIELD_ERRORS = {
    room_number: "Укажите номер комнаты",
    category_id: "Выберите службу",
    summary: "Напишите, что нужно сделать"
  };
  var OFFLINE = "Нет связи — попробуйте ещё раз.";

  function postJson(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  function bind(form) {
    var dialogMode = form.dataset.mode === "dialog";
    var win = dialogMode ? form.closest("[data-window]") : null;
    var holder = form.querySelector("[data-new-request-fields]");
    var status = form.querySelector("[data-form-status]");
    var submit = form.querySelector("button[type=submit]");
    /* Номер загрузки полей: ответ на прошлое открытие окна не затирает новое. */
    var loadSeq = 0;

    function field(selector) {
      return form.querySelector(selector);
    }

    function showStatus(text) {
      status.textContent = text;
      status.hidden = !text;
    }

    function fieldError(name, text) {
      var error = field('[data-field-error="' + name + '"]');
      if (!error) return;
      error.textContent = text;
      error.hidden = !text;
      form.querySelectorAll('[name="' + name + '"]').forEach(function (input) {
        if (text) input.setAttribute("aria-invalid", "true");
        else input.removeAttribute("aria-invalid");
      });
    }

    function chosenService() {
      return field("input[name=category_id]:checked");
    }

    /* Чужая служба (spec 0045 §3): флажок гаснет сразу, а не отказом после создания. */
    function syncClaim() {
      var claim = field("[data-claim-toggle]");
      if (!claim) return;
      var chosen = chosenService();
      var foreign = Boolean(chosen && chosen.hasAttribute("data-foreign"));
      claim.disabled = foreign;
      if (foreign) claim.checked = false;
      field("[data-claim-foreign]").hidden = !foreign;
    }

    /* Все пустые поля разом, фокус — на первое сверху. */
    function collect() {
      var room = field("input[name=room_number]");
      var summary = field("textarea[name=summary]");
      var chosen = chosenService();
      var values = {
        room_number: room.value.trim(),
        category_id: chosen ? chosen.value : "",
        summary: summary.value.trim()
      };
      var first = null;
      [["room_number", room], ["category_id", field("input[name=category_id]")],
        ["summary", summary]].forEach(function (pair) {
        if (values[pair[0]]) return;
        fieldError(pair[0], FIELD_ERRORS[pair[0]]);
        if (!first) first = pair[1];
      });
      if (first) {
        first.focus();
        return null;
      }
      return values;
    }

    /* Взять только что созданную; не вышло — заявка всё равно создана. */
    async function claimCreated(id) {
      try {
        var response = await postJson(form.dataset.actionsEndpoint + "/" + id + "/claim", {});
        return response.ok;
      } catch (error) {
        return false;
      }
    }

    function createdText(number, chosen, wanted, taken) {
      var label = number ? "Заявка #" + number : "Заявка";
      if (chosen.hasAttribute("data-foreign")) {
        return label + " создана и ушла в службу «" + chosen.dataset.serviceName + "»";
      }
      if (wanted && taken) return label + " создана — она ваша";
      if (wanted) return label + " создана, но взять её не получилось — она ждёт в очереди";
      return label + " создана";
    }

    async function loadFields() {
      var seq = ++loadSeq;
      holder.replaceChildren();
      showStatus("");
      submit.disabled = true;
      var html = null;
      try {
        var response = await fetch(form.dataset.fieldsUrl, { cache: "no-store" });
        if (response.redirected) {
          /* Сессия истекла: фрагмент ответил редиректом на вход — уходим туда же. */
          window.location.href = response.url;
          return;
        }
        if (response.ok) html = await response.text();
        else showStatus(response.status === 403 ? MESSAGES["ERR-AUTH-003"] : "Не получилось открыть форму — попробуйте ещё раз.");
      } catch (error) {
        showStatus(OFFLINE);
      }
      if (seq !== loadSeq || html === null || win.hidden) return;
      holder.innerHTML = html;
      syncClaim();
      submit.disabled = !field("input[name=category_id]");
      var room = field("input[name=room_number]");
      if (room) room.focus();
    }

    form.addEventListener("change", function (event) {
      if (event.target.name !== "category_id") return;
      fieldError("category_id", "");
      syncClaim();
    });
    form.addEventListener("input", function (event) {
      var name = event.target.name;
      if ((name === "room_number" || name === "summary") && event.target.value.trim()) {
        fieldError(name, "");
      }
    });
    if (dialogMode) win.addEventListener("staff-window-open", loadFields);

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (submit.disabled) return;
      Object.keys(FIELD_ERRORS).forEach(function (name) { fieldError(name, ""); });
      showStatus("");
      var payload = collect();
      if (!payload) return;
      var chosen = chosenService();
      var claim = field("[data-claim-toggle]");
      var wanted = claim.checked && !claim.disabled;
      /* Кнопка гаснет на время запроса: второе нажатие завело бы вторую заявку
         (дневной номер выдаёт сервер, повтор он не склеит). */
      submit.disabled = true;
      var data = null;
      try {
        var response = await postJson(form.dataset.endpoint, payload);
        try { data = await response.json(); } catch (error) { /* не конверт */ }
        if (!response.ok) {
          var code = data && data.error && data.error.code;
          showStatus(MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.");
          submit.disabled = false;
          return;
        }
      } catch (error) {
        showStatus(OFFLINE);
        submit.disabled = false;
        return;
      }
      var taken = wanted && data && data.id ? await claimCreated(data.id) : false;
      var number = data && data.daily_number;
      if (!dialogMode) {
        /* Плашку с номером рисует страница очереди; служба — чтобы плашка назвала
         * чужую: в очереди такой заявки не будет (spec 0045 §3). */
        var queuePath = form.dataset.queuePath;
        window.location.href = number
          ? queuePath + "?created=" + number + "&service=" + encodeURIComponent(data.category_id)
          : queuePath;
        return;
      }
      window.StaffWindows.close();
      window.StaffWindows.toast(createdText(number, chosen, wanted, taken), null);
      document.dispatchEvent(new Event("staff-request-created"));
      /* Сигнал кабинета (signal.js) перечитывает невзятые сразу. */
      document.dispatchEvent(new Event("staff-queue-changed"));
    });
  }

  document.querySelectorAll("[data-new-request-form]").forEach(bind);
})();
