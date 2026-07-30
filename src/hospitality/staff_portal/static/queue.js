/* Очередь заявок кабинета (spec 0033 §5, PR D серии #48).
 *
 * Ванильный JS без сборки (ADR-014): поллинг списка каждые 15 с + JSON-действия
 * «взять» / «готово» / «отменить». Отдельный файл, не inline-скрипт — CSP
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
  var fragmentUrl = list.dataset.fragmentUrl;

  /* Дружелюбные тексты по кодам каталога ошибок (R-8): «уже взята» — spec 0033 §8. */
  var MESSAGES = {
    "ERR-REQUESTS-003": "Заявка уже взята или закрыта — список обновлён.",
    "ERR-REQUESTS-002": "Заявка не найдена — список обновлён.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию."
  };

  function showStatus(text) {
    status.textContent = text;
    status.hidden = !text;
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

  async function post(card, action, note) {
    card.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
    showStatus("");
    try {
      var response = await fetch(endpoint + "/" + card.dataset.requestId + "/" + action, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(note ? { note: note } : {})
      });
      if (!response.ok) {
        var code = "";
        try { code = (await response.json()).error.code; } catch (error) { /* не конверт */ }
        showStatus(MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.");
      }
    } catch (error) {
      showStatus("Нет связи — попробуйте ещё раз.");
    }
    /* И при успехе, и при конфликте показываем актуальное состояние очереди. */
    await refresh();
  }

  function openNoteForm(card, action) {
    var form = card.querySelector("[data-note-form]");
    if (!form) return;
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
    var noteCancel = event.target.closest("[data-note-cancel]");
    if (noteCancel) {
      noteCancel.closest("[data-note-form]").hidden = true;
      return;
    }
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var card = button.closest(".request-card");
    var action = button.dataset.action;
    if (action === "claim") {
      post(card, "claim", null);
    } else {
      openNoteForm(card, action);
    }
  });

  list.addEventListener("submit", function (event) {
    var form = event.target.closest("[data-note-form]");
    if (!form) return;
    event.preventDefault();
    var card = form.closest(".request-card");
    var note = form.querySelector("input[name=note]").value.trim();
    if (form.dataset.pending === "cancel" && !note) {
      showStatus("Укажите причину отмены.");
      return;
    }
    post(card, form.dataset.pending, note || null);
  });
})();
