/* Форма «Новая заявка» кабинета (spec 0035 §5, PR C серии #299).
 *
 * Ванильный JS без сборки (ADR-014, канон queue.js): проверка трёх полей
 * текстами §9, один POST по CSRF-контракту (Content-Type: application/json +
 * Origin от fetch), успех — возврат в очередь с плашкой о номере заявки.
 * Разметку JS не рисует: форма — Jinja (new_request.html).
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-new-request-form]");
  if (!form) return;
  var endpoint = form.dataset.endpoint;
  var queuePath = form.dataset.queuePath;
  var status = form.querySelector("[data-form-status]");
  var submit = form.querySelector("button[type=submit]");

  /* Дружелюбные тексты по кодам каталога ошибок (R-8), канон queue.js. */
  var MESSAGES = {
    "ERR-REQUESTS-001": "Такой службы у отеля нет — обновите страницу.",
    "ERR-PLATFORM-002": "Проверьте поля — заявку не приняли.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию."
  };

  function showStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  /* Три текста отказа — §9 спеки; порядок сверху вниз по форме. */
  function collect() {
    var room = form.querySelector("input[name=room_number]").value.trim();
    if (!room) {
      showStatus("Укажите номер комнаты");
      return null;
    }
    var category = form.querySelector("input[name=category_id]:checked");
    if (!category) {
      showStatus("Выберите службу");
      return null;
    }
    var summary = form.querySelector("textarea[name=summary]").value.trim();
    if (!summary) {
      showStatus("Напишите, что нужно сделать");
      return null;
    }
    return { room_number: room, category_id: category.value, summary: summary };
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    showStatus("");
    var payload = collect();
    if (!payload) return;
    /* Кнопка гаснет на время запроса: второе нажатие завело бы вторую заявку
       (дневной номер выдаёт сервер, повтор он не склеит). */
    submit.disabled = true;
    try {
      var response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не конверт */ }
      if (!response.ok) {
        var code = data && data.error && data.error.code;
        showStatus(MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.");
        submit.disabled = false;
        return;
      }
      /* Плашку с номером рисует страница очереди: заявка уже создана, и
         показывать её нужно там, куда сотрудник смотрит дальше. */
      var created = data && data.daily_number;
      window.location.href = created ? queuePath + "?created=" + created : queuePath;
    } catch (error) {
      showStatus("Нет связи — попробуйте ещё раз.");
      submit.disabled = false;
    }
  });
})();
