/* Документы для АВР (spec 0049 §6, Р-5; issue #493).
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Разметка строк живёт только в Jinja (act_requests.html). Поллинга нет: он
 * затёр бы правку бухгалтера; после действия страница перезагружается.
 *
 * Правка в строке шлёт только изменённые поля — неизменённое поле не попадает
 * в «правил человек» и не снимает «Проверено» зря. «Взять», «Готово» и
 * «Вернуть гостю» — те же JSON-действия, что у очереди (по id заявки).
 *
 * CSRF-контракт JSON-действий (докстринг router.py): каждый POST несёт
 * Content-Type: application/json — Origin браузер добавляет сам.
 */
(function () {
  "use strict";

  var table = document.getElementById("act-table");
  var status = document.getElementById("act-status");
  if (!table || !status) return;
  var endpoint = table.dataset.endpoint;

  /* Дружелюбные тексты по кодам каталога ошибок (R-8). */
  var MESSAGES = {
    "ERR-DOCUMENTS-004": "Запрос не найден — возможно, срок хранения истёк. Страница обновлена.",
    "ERR-DOCUMENTS-006": "Проверьте формат: БИН/ИИН — 12 цифр, ИИК — KZ и ещё 18 знаков, " +
      "БИК — 8 знаков, например HSBKKZKX.",
    "ERR-FILES-005": "Реквизиты сейчас недоступны — сообщите администратору (ERR-FILES-005).",
    "ERR-REQUESTS-003": "Запрос уже взяли или закрыли — страница обновлена.",
    "ERR-REQUESTS-005": "Запрос уже закрыли или передали — страница обновлена.",
    "ERR-REQUESTS-007": "Заявку этого запроса передали в другую службу.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию."
  };
  /* Коды, после которых страница устарела и перезагружается сама. */
  var STALE = ["ERR-DOCUMENTS-004", "ERR-REQUESTS-003", "ERR-REQUESTS-005"];

  function showStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  /* POST по CSRF-контракту; вернуть код ошибки каталога или "" при успехе. */
  async function send(url, body) {
    try {
      var response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (response.ok) return "";
      try { return (await response.json()).error.code || String(response.status); }
      catch (error) { return String(response.status); }
    } catch (error) {
      return "network";
    }
  }

  async function act(group, url, body) {
    group.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
    showStatus("");
    var code = await send(url, body);
    if (!code || STALE.indexOf(code) !== -1) {
      window.location.reload();
      return;
    }
    showStatus(code === "network" ? "Нет связи — попробуйте ещё раз."
      : MESSAGES[code] || "Не получилось (" + code + "). Попробуйте ещё раз.");
    group.querySelectorAll("button").forEach(function (button) { button.disabled = false; });
  }

  /* ---------- Правка в строке ---------- */

  function setEditing(group, editing) {
    group.classList.toggle("act-editing", editing);
    group.querySelectorAll("input[data-input]").forEach(function (input) {
      input.hidden = !editing;
      if (!editing) input.value = input.dataset.original;
    });
    group.querySelector("[data-action=edit]").hidden = editing;
    group.querySelector("[data-action=save]").hidden = !editing;
    group.querySelector("[data-action=edit-cancel]").hidden = !editing;
  }

  function changedFields(group) {
    var changed = {};
    group.querySelectorAll("input[data-input]").forEach(function (input) {
      var value = input.value.trim();
      if (value !== input.dataset.original) changed[input.name] = value;
    });
    return changed;
  }

  /* ---------- Примечание к «Готово» и «Вернуть гостю» ---------- */

  function openNoteForm(group, action) {
    var form = group.querySelector("[data-note-form]");
    form.dataset.pending = action;
    form.querySelector("[data-note-label]").textContent = action === "cancel"
      ? "Что уточнить? Гость прочитает это в чате"
      : "Примечание для гостя (необязательно)";
    form.hidden = false;
    form.querySelector("input[name=note]").focus();
  }

  table.addEventListener("click", function (event) {
    var formCancel = event.target.closest("[data-form-cancel]");
    if (formCancel) {
      formCancel.closest("form").hidden = true;
      return;
    }
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var group = button.closest("[data-act-id]");
    var action = button.dataset.action;
    var actUrl = endpoint + "/act-requests/" + group.dataset.actId;
    if (action === "edit") {
      setEditing(group, true);
      group.querySelector("input[data-input]").focus();
    } else if (action === "edit-cancel") {
      setEditing(group, false);
    } else if (action === "save") {
      var changed = changedFields(group);
      if (Object.keys(changed).length === 0) {
        setEditing(group, false);
        return;
      }
      act(group, actUrl + "/requisites", changed);
    } else if (action === "checked") {
      act(group, actUrl + "/checked", {});
    } else if (action === "claim") {
      act(group, endpoint + "/requests/" + group.dataset.requestId + "/claim", {});
    } else {
      openNoteForm(group, action);
    }
  });

  table.addEventListener("submit", function (event) {
    var form = event.target.closest("[data-note-form]");
    if (!form) return;
    event.preventDefault();
    var group = form.closest("[data-act-id]");
    var note = form.querySelector("input[name=note]").value.trim();
    if (form.dataset.pending === "cancel" && !note) {
      showStatus("Напишите, что уточнить, — гость прочитает это в чате.");
      return;
    }
    act(group, endpoint + "/requests/" + group.dataset.requestId + "/" + form.dataset.pending,
      note ? { note: note } : {});
  });
})();
