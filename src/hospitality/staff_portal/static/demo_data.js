/* «Тестовые данные» (issue #740): кнопка → окно «Заменить тестовые данные на
 * свежие?» → один POST по CSRF-контракту (Content-Type: application/json +
 * Origin от fetch). Пока идёт — «Заполняю…» и кнопка погашена: второе нажатие
 * построило бы картину ещё раз. Итог — текст сервера (`message`), ошибка —
 * общий текст и код из конверта; только textContent. Разметку JS не рисует —
 * канон knowledge.js; окно открывает и закрывает app_windows.js.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-demo-data]");
  var fillButton = document.querySelector("[data-demo-data-fill]");
  if (!root || !fillButton || !window.StaffWindows) return;
  var openButton = root.querySelector("[data-demo-data-open]");
  var label = root.querySelector("[data-demo-data-label]");
  var done = root.querySelector("[data-demo-data-done]");
  var failed = root.querySelector("[data-demo-data-error]");
  var IDLE = label.textContent;
  var FAILED = "Не получилось заполнить. Попробуйте ещё раз через минуту.";

  function show(element, text) {
    element.textContent = text;
    element.hidden = !text;
  }

  function setBusy(busy) {
    openButton.disabled = busy;
    label.textContent = busy ? "Заполняю…" : IDLE;
  }

  async function fill() {
    window.StaffWindows.close();
    setBusy(true);
    show(done, "");
    show(failed, "");
    try {
      var response = await fetch(root.dataset.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не JSON */ }
      if (response.ok && data && data.message) {
        show(done, data.message);
      } else {
        var code = data && data.error && data.error.code;
        show(failed, FAILED + " (" + (code || response.status) + ")");
      }
    } catch (error) {
      show(failed, FAILED);
    }
    setBusy(false);
  }

  openButton.addEventListener("click", function () {
    window.StaffWindows.open("demo-data-confirm", openButton);
  });
  fillButton.addEventListener("click", fill);
})();
