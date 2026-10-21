/* Справочник отеля (spec 0036 §7, §8.1; issue #335).
 *
 * Ванильный JS без сборки (ADR-014, канон team.js): «Изменить» раскрывает
 * форму на карточке, «Удалить» спрашивает подтверждение, все три действия —
 * POST по CSRF-контракту (Content-Type: application/json + Origin от fetch),
 * после успеха страница перезагружается. Разметку JS не рисует — только
 * прячет и показывает готовую из knowledge.html.
 *
 * ОСОЗНАННОЕ отступление от канона MESSAGES[code] (spec 0036 §7): у
 * ERR-PLATFORM-009 десять причин под одним кодом, и надпись каждой — по-русски
 * менеджеру в error.message конверта. Только для этого кода текст берётся из
 * ответа; у прочих кодов message английский и написан разработчику.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-endpoint]");
  if (!root) return;
  var endpoint = root.dataset.endpoint;

  var REJECTED = "ERR-PLATFORM-009";
  var MESSAGES = {
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию.",
    "ERR-AUTH-009": "Страница устарела — обновите её.",
    "ERR-PLATFORM-002": "Проверьте поля — факт не сохранили.",
    "ERR-PLATFORM-005": "Отель ещё не настроен — справочник появится после настройки."
  };

  function setBusy(busy) {
    document.querySelectorAll("button").forEach(function (button) { button.disabled = busy; });
  }

  function show(status, text) {
    status.textContent = text;
    status.hidden = !text;
  }

  async function post(path, payload) {
    var response = await fetch(endpoint + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    if (response.ok) return null;
    var data = null;
    try { data = await response.json(); } catch (error) { /* не конверт */ }
    var error = data && data.error;
    var code = error && error.code;
    if (code === REJECTED && error.message) return error.message;
    return MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.";
  }

  async function act(path, payload, status) {
    setBusy(true);
    show(status, "");
    try {
      var failure = await post(path, payload);
      if (!failure) {
        window.location.reload();
        return;
      }
      show(status, failure);
    } catch (error) {
      show(status, "Нет связи — попробуйте ещё раз.");
    }
    setBusy(false);
  }

  /* Пустые поля ловятся до отправки — те же надписи §8.1, что у сервера
     (канон new_request.js); серверная проверка остаётся: страница не
     единственный вход. */
  function collect(form, status) {
    var topic = form.querySelector("[name=topic]").value.trim();
    var answer = form.querySelector("[name=answer]").value.trim();
    if (!topic) {
      show(status, "Напишите тему — одно-два слова: «Завтрак», «Wi-Fi»");
      return null;
    }
    if (!answer) {
      show(status, "Напишите ответ гостю — то, что бот скажет на этот вопрос");
      return null;
    }
    return {
      topic: topic,
      answer: answer,
      valid_until: form.querySelector("[name=valid_until]").value || null
    };
  }

  document.querySelectorAll("[data-fact-form]").forEach(function (form) {
    var status = form.querySelector("[data-form-status]");
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      show(status, "");
      var fact = collect(form, status);
      if (!fact) return;
      if (form.dataset.mode === "add") {
        act("", fact, status);
        return;
      }
      act("/update", {
        topic: form.dataset.topic,
        new_topic: fact.topic,
        answer: fact.answer,
        valid_until: fact.valid_until
      }, status);
    });
  });

  document.querySelectorAll("[data-fact]").forEach(function (card) {
    var view = card.querySelector("[data-fact-view]");
    var form = card.querySelector("[data-fact-form]");
    card.addEventListener("click", function (event) {
      var button = event.target.closest("[data-action]");
      if (!button) return;
      var action = button.dataset.action;
      if (action === "edit" || action === "cancel") {
        view.hidden = action === "edit";
        form.hidden = action !== "edit";
        if (action === "edit") form.querySelector("[name=topic]").focus();
        return;
      }
      if (action === "delete") {
        var topic = card.dataset.topic;
        if (!window.confirm("Удалить «" + topic + "» из справочника? Бот перестанет отвечать гостям на эту тему.")) return;
        act("/delete", { topic: topic }, view.querySelector("[data-view-status]"));
      }
    });
  });
})();
