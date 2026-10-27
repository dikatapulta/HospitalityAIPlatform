/* Доска «Выезды и уборка» (spec 0046 §5–§6, issue #476).
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Разметка строк живёт только в Jinja (_turnover_board.html): обновление —
 * замена innerHTML контейнера ответом fragment-эндпоинта каждые 15 с, пауза на
 * скрытой вкладке и ПОКА ОТКРЫТО ПОЛЕ ЗАМЕЧАНИЯ: поллинг не затирает ввод
 * горничной (урок #221).
 *
 * CSRF-контракт JSON-действий (докстринг router.py): каждый POST несёт
 * Content-Type: application/json — Origin браузер добавляет сам.
 */
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 15000;

  var board = document.getElementById("turnover-board");
  var status = document.getElementById("turnover-status");
  if (!board || !status) return;
  var endpoint = board.dataset.endpoint;
  var fragmentUrl = board.dataset.fragmentUrl;
  var reportForm = document.getElementById("turnover-report");

  var NO_HOUSEKEEPING = "В кабинете пока нет сотрудников хаускипинга — выезд здесь никто " +
    "не увидит. Сообщите им, как раньше.";

  /* Дружелюбные тексты по кодам каталога ошибок (R-8). Отказ по праву зависит
   * от действия: кнопок не по праву на доске нет, 403 значит — права сменились. */
  var MESSAGES = {
    "ERR-HOUSEKEEPING-001": "Этого выезда уже нет на доске — список обновлён.",
    "ERR-HOUSEKEEPING-002": "Этот номер уже отметили — список обновлён.",
    "ERR-HOUSEKEEPING-005": NO_HOUSEKEEPING,
    "ERR-GUESTS-001": "Гостя в этом номере уже выселили — отметьте выезд ещё раз.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново."
  };
  var FORBIDDEN = {
    accept: "Принимает номер старший хаускипинга или менеджер.",
    cancel: "Отменить выезд может ресепшен, старший хаускипинга или менеджер.",
    report: "Отметить выезд может ресепшен или менеджер."
  };
  var FORBIDDEN_DEFAULT = "Отмечать уборку может хаускипинг.";

  function showStatus(text, ok) {
    status.textContent = text;
    status.className = ok ? "hint ok" : "error";
    status.hidden = !text;
  }

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

  function failureText(result, action) {
    if (result.code === "network") return "Нет связи — попробуйте ещё раз.";
    if (result.code === "ERR-HOUSEKEEPING-003") return FORBIDDEN[action] || FORBIDDEN_DEFAULT;
    return MESSAGES[result.code] ||
      "Не получилось (" + (result.code || result.status) + "). Попробуйте ещё раз.";
  }

  /* ---------- Отметить выезд ---------- */

  async function report(room, checkOutStay) {
    var result = await send(endpoint, { room_number: room, check_out_stay: checkOutStay });
    if (!result.ok && result.code === "ERR-HOUSEKEEPING-004") {
      var question = "В кабинете в " + room + " заселён гость. Выселить его и отправить " +
        "номер на уборку? Доступ к чату сразу погаснет.";
      if (window.confirm(question)) return report(room, true);
      return null;
    }
    return result;
  }

  if (reportForm) {
    reportForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var input = reportForm.querySelector("input[name=room]");
      var room = input.value.trim();
      if (!room) {
        showStatus("Укажите номер комнаты.");
        input.focus();
        return;
      }
      var button = reportForm.querySelector("button[type=submit]");
      button.disabled = true;
      showStatus("");
      try {
        var result = await report(room, false);
        if (result === null) return;
        if (!result.ok) {
          showStatus(failureText(result, "report"));
          return;
        }
        input.value = "";
        var data = result.data;
        showStatus(data.created
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
    var result = await send(endpoint + "/" + row.dataset.turnoverId + "/" + action, body);
    if (!result.ok) showStatus(failureText(result, action));
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
