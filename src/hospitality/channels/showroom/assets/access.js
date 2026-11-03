/* Экран кода доступа 3D-витрины (spec 0069 §2.2, §5): POST {code} → cookie → страница.
 * 403 — «Код не подошёл», 429 — «Слишком много попыток…»; тексты — в data-атрибутах
 * формы, на языке страницы. Только textContent.
 */
(function () {
  "use strict";
  var form = document.querySelector("[data-sr-access]");
  if (!form) return;
  var input = form.querySelector("input");
  var button = form.querySelector("button");
  var error = form.querySelector("[data-sr-access-error]");

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    button.disabled = true;
    error.hidden = true;
    try {
      var response = await fetch(form.dataset.endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: input.value }),
      });
      if (response.ok) {
        window.location.reload();
        return;
      }
      error.textContent = response.status === 429 ? form.dataset.limited : form.dataset.wrong;
    } catch (failure) {
      error.textContent = form.dataset.wrong;
    }
    error.hidden = false;
    button.disabled = false;
  });
})();
