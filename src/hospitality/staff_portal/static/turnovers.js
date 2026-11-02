/* Доска «Выезды и уборка» (spec 0046 §5–§6, issue #476).
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Разметка строк живёт только в Jinja (_turnover_board.html): обновление —
 * замена innerHTML контейнера ответом fragment-эндпоинта каждые 15 с, пауза на
 * скрытой вкладке и ПОКА ОТКРЫТО ПОЛЕ ЗАМЕЧАНИЯ: поллинг не затирает ввод
 * горничной (урок #221).
 *
 * Запросы, тексты ошибок и вопрос выселения — turnover_actions.js (общий с
 * доской «Номера», подключается раньше этого файла).
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 15000;

  var board = document.getElementById("turnover-board");
  var status = document.getElementById("turnover-status");
  var api = window.TurnoverActions;
  if (!board || !status || !api) return;
  var endpoint = board.dataset.endpoint;
  var fragmentUrl = board.dataset.fragmentUrl;
  var reportForm = document.getElementById("turnover-report");
  /* Ответ формы «Отметить выезд» — под её полем (#639); ошибка шага доски —
   * строкой над доской (#turnover-status). */
  var reportStatus = document.getElementById("turnover-report-status");

  function showIn(element, text, ok) {
    element.textContent = text;
    element.className = ok ? "hint ok" : "error";
    element.hidden = !text;
  }

  function showStatus(text, ok) { showIn(status, text, ok); }

  function remarkOpen() {
    return Array.prototype.some.call(
      board.querySelectorAll("[data-remark-form]"),
      function (form) { return !form.hidden; }
    );
  }

  var refreshing = false;
  async function refresh(force) {
    if (refreshing || (!force && remarkOpen())) return;
    refreshing = true;
    try {
      var response = await fetch(fragmentUrl, { cache: "no-store" });
      if (response.redirected) {
        /* Сессия истекла: фрагмент ответил редиректом на вход — уходим туда же. */
        window.location.href = response.url;
        return;
      }
      if (response.ok) board.innerHTML = await response.text();
    } catch (error) {
      /* Нет сети — тик пропущен, следующий поллинг попробует снова. */
    } finally {
      refreshing = false;
    }
  }

  setInterval(function () {
    if (!document.hidden) refresh(false);
  }, POLL_INTERVAL_MS);

  /* ---------- Отметить выезд ---------- */

  if (reportForm && reportStatus) {
    var input = reportForm.querySelector("input[name=room]");
    input.addEventListener("input", function () { input.removeAttribute("aria-invalid"); });
    reportForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var room = input.value.trim();
      input.removeAttribute("aria-invalid");
      if (!room) {
        input.setAttribute("aria-invalid", "true");
        showIn(reportStatus, "Укажите номер комнаты.");
        input.focus();
        return;
      }
      var button = reportForm.querySelector("button[type=submit]");
      button.disabled = true;
      showIn(reportStatus, "");
      try {
        var result = await api.report(endpoint, room, false);
        if (result === null) return;
        if (!result.ok) {
          showIn(reportStatus, api.failureText(result, "report"));
          return;
        }
        input.value = "";
        var data = result.data;
        showIn(reportStatus, data.created
          ? data.room_number + " — выезд отмечен, хаускипинг уже видит."
          : data.room_number + " уже у хаускипинга — с " + data.since + ".", true);
      } finally {
        button.disabled = false;
        await refresh(true);
      }
    });
  }

  /* ---------- Шаги на доске ---------- */

  async function step(row, action, body) {
    row.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
    showStatus("");
    var result = await api.send(endpoint + "/" + row.dataset.turnoverId + "/" + action, body);
    if (!result.ok) showStatus(api.failureText(result, action));
    /* И при успехе, и при конфликте — актуальная доска; сигнал кабинета
     * (signal.js) перечитывает счётчик номеров сразу, а не через 15 с. */
    document.dispatchEvent(new Event("staff-turnovers-changed"));
    await refresh(true);
  }

  board.addEventListener("click", function (event) {
    var formCancel = event.target.closest("[data-form-cancel]");
    if (formCancel) {
      formCancel.closest("form").hidden = true;
      return;
    }
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var row = button.closest("[data-turnover-id]");
    var action = button.dataset.action;
    if (action === "remark") {
      var form = row.querySelector("[data-remark-form]");
      form.hidden = false;
      form.querySelector("textarea").focus();
      return;
    }
    if (action === "cancel" &&
        !window.confirm("Отменить выезд из " + row.dataset.room + "? Номер уйдёт с доски.")) {
      return;
    }
    step(row, action, {});
  });

  board.addEventListener("submit", function (event) {
    var form = event.target.closest("[data-remark-form]");
    if (!form) return;
    event.preventDefault();
    var note = form.querySelector("textarea").value.trim();
    if (!note) {
      showStatus("Напишите, что не так, или нажмите «Проверено».");
      return;
    }
    form.hidden = true;
    step(form.closest("[data-turnover-id]"), "inspect", { note: note });
  });
})();
