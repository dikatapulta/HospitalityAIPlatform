/* «Наставник» → «Итоги экзаменов»: «Назначить экзамен» и «Снять» (spec 0072 §3; #898).
 *
 * Форма шлёт JSON в POST {api}: chapters — номера отмеченных глав или null (весь кодекс),
 * service_keys — отмеченные службы или null (всем); «отметьте хотя бы одну» проверяется
 * до отправки. «Снять» — POST {api}/{id}/archive после подтверждения. Успех — страница
 * перезагружается: разделы и карточки служб собирает сервер (templates/mentor_results.html).
 * Адрес и тексты — #mentor-assign-state; всё — узлами DOM, без innerHTML.
 */
(function () {
  "use strict";

  var stateNode = document.getElementById("mentor-assign-state");
  if (!stateNode) return;
  var state = JSON.parse(stateNode.textContent);
  var texts = state.texts;
  var api = state.api;
  var form = document.getElementById("mentor-assign");  /* нет кодекса — формы нет */
  var busy = false;

  function part(name) { return form.querySelector('[data-assign-part="' + name + '"]'); }
  function error(name) { return document.querySelector('[data-assign-error="' + name + '"]'); }
  function checked(name) {
    return Array.prototype.map.call(
      form.querySelectorAll('input[name="' + name + '"]:checked'),
      function (input) { return input.value; });
  }

  async function post(url, body) {
    try {
      var response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (response.status === 401) window.location.reload();  /* сессия истекла */
      return response.ok;
    } catch (failure) {
      return false;  /* нет связи — тот же текст, что при отказе */
    }
  }

  if (form) {
    /* Радиокнопка «Выбрать главы» / «Службам» показывает свой список галочек. */
    form.addEventListener("change", function (event) {
      var input = event.target;
      if (input.name === "topic") part("chapters").hidden = input.value !== "chapters";
      if (input.name === "audience") part("services").hidden = input.value !== "services";
      if (input.name === "chapter") error("chapters").hidden = true;
      if (input.name === "service_keys") error("services").hidden = true;
    });

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      if (busy) return;
      var byChapters = form.querySelector('input[name="topic"]:checked').value === "chapters";
      var byServices = form.querySelector('input[name="audience"]:checked').value === "services";
      var chapters = byChapters ? checked("chapter").map(Number) : null;
      var services = byServices ? checked("service_keys") : null;
      error("chapters").hidden = !(chapters && !chapters.length);
      error("services").hidden = !(services && !services.length);
      error("failed").hidden = true;
      if ((chapters && !chapters.length) || (services && !services.length)) return;
      busy = true;
      var ok = await post(api, { chapters: chapters, service_keys: services });
      busy = false;
      if (ok) window.location.reload();
      else error("failed").hidden = false;
    });
  }

  document.querySelectorAll("button[data-archive]").forEach(function (button) {
    button.addEventListener("click", async function () {
      if (busy || !window.confirm(texts.archive_confirm)) return;
      busy = true;
      var ok = await post(api + "/" + button.dataset.archive + "/archive", {});
      busy = false;
      if (ok) window.location.reload();
      else if (window.StaffWindows) window.StaffWindows.toast(texts.failed, null);
    });
  });
})();
