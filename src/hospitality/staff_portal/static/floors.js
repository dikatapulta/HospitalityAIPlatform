/* Страница «Этажи» (spec 0053 §5, issue #478): «Сохранить раздачу».
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Разбирает поле сервер (одна грамматика — floors.parse_floors): ошибка разбора
 * приходит обычной ошибкой проверки запроса (ERR-PLATFORM-002) с номером строки
 * в loc — по нему страница называет горничную и её текст. После сохранения —
 * перезагрузка с ?saved=1: строка состояния и «По этажам» рисует только Jinja.
 *
 * CSRF-контракт JSON-действий (докстринг router.py): каждый POST несёт
 * Content-Type: application/json — Origin браузер добавляет сам.
 */
(function () {
  "use strict";

  var form = document.getElementById("floors-form");
  var status = document.getElementById("floors-status");
  if (!form || !status) return;
  var endpoint = form.dataset.endpoint;
  var inputs = form.querySelectorAll("input[data-user-id]");
  var button = form.querySelector("button[type=submit]");

  var EXAMPLE = " Пишите так: 7, 8, 12-14. Этажи — от 1 до 99.";
  var MESSAGES = {
    "ERR-HOUSEKEEPING-003": "Раздаёт этажи старший хаускипинга или менеджер.",
    "ERR-HOUSEKEEPING-006": "Состав хаускипинга изменился — обновите страницу и раздайте ещё раз.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново."
  };

  function showStatus(text, ok) {
    status.textContent = text;
    status.className = ok ? "hint ok" : "error";
    status.hidden = !text;
  }

  /* POST по CSRF-контракту; вернуть { ok, code, data }. */
  async function send(url, body) {
    try {
      var response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не JSON */ }
      var code = !response.ok && data && data.error ? data.error.code : "";
      return { ok: response.ok, status: response.status, code: code, data: data };
    } catch (error) {
      return { ok: false, status: 0, code: "network", data: null };
    }
  }

  /* Номер строки с непонятыми этажами: loc ошибки — ["body", "rows", i, "floors"]. */
  function badRowIndex(data) {
    var details = data && data.error && data.error.details;
    if (!details) return -1;
    for (var k = 0; k < details.length; k++) {
      var loc = details[k].loc || [];
      if (loc[1] === "rows" && typeof loc[2] === "number") return loc[2];
    }
    return -1;
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    Array.prototype.forEach.call(inputs, function (input) {
      input.removeAttribute("aria-invalid");
    });
    var rows = Array.prototype.map.call(inputs, function (input) {
      return { user_id: input.dataset.userId, floors: input.value };
    });
    showStatus("", false);
    button.disabled = true;
    var result = await send(endpoint, { rows: rows });
    if (result.ok) {
      window.location.href = window.location.pathname + "?saved=1";
      return;
    }
    button.disabled = false;
    var index = result.code === "ERR-PLATFORM-002" ? badRowIndex(result.data) : -1;
    if (index >= 0 && inputs[index]) {
      var input = inputs[index];
      input.setAttribute("aria-invalid", "true");
      input.focus();
      showStatus("Не поняли этажи у " + input.dataset.name + ": «" + input.value.trim() + "»." +
        EXAMPLE, false);
      return;
    }
    if (result.code === "network") {
      showStatus("Нет связи — попробуйте ещё раз.", false);
      return;
    }
    showStatus(MESSAGES[result.code] ||
      "Не получилось (" + (result.code || result.status) + "). Попробуйте ещё раз.", false);
  });
})();
