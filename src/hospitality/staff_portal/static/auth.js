/* Страницы до входа (кабинет v3, #637): кнопка-глаз «Показать пароль».
 *
 * Разметки JS не рисует: кнопка — в _auth_fields.html со спрятанным `hidden`,
 * здесь её показывают и ведут (канон shell.js). Без JS кнопки нет, поле — как
 * обычное. Перед отправкой формы пароль снова скрывается: менеджер паролей
 * браузера узнаёт поле пароля по type="password" и иначе не предложит его
 * сохранить.
 */
(function () {
  "use strict";

  var SHOW = "Показать пароль";
  var HIDE = "Скрыть пароль";

  function setShown(button, input, shown) {
    input.type = shown ? "text" : "password";
    button.toggleAttribute("data-shown", shown);
    button.setAttribute("aria-label", shown ? HIDE : SHOW);
    button.title = shown ? HIDE : SHOW;
  }

  document.querySelectorAll("[data-password-toggle]").forEach(function (button) {
    var input = document.getElementById(button.getAttribute("aria-controls"));
    if (!input) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      setShown(button, input, input.type === "password");
      input.focus();
    });
    if (input.form) {
      input.form.addEventListener("submit", function () {
        setShown(button, input, false);
      });
    }
  });
})();
