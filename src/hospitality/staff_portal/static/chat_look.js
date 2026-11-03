/* «Вид гостевого чата» (spec 0066 §6): выбор цвета и узора сразу меняет оба телефона
 * предпросмотра (новый адрес iframe), в БД — по «Сохранить»: один POST по CSRF-контракту
 * (Content-Type: application/json + Origin от fetch). Знак — файл в multipart/form-data,
 * Origin браузер добавляет сам; после загрузки или «Убрать» страница перезагружается —
 * знак стоит и в карточке, и в телефонах. Тексты ошибок — `message` конверта сервера,
 * его нет — общий; только textContent. Разметку JS не рисует — канон knowledge.js.
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-chat-look]");
  var logo = document.querySelector("[data-logo]");
  if (!form || !logo) return;
  var endpoint = form.dataset.endpoint;
  var done = form.querySelector("[data-chat-look-done]");
  var failed = form.querySelector("[data-chat-look-error]");
  var saveButton = form.querySelector("button[type=submit]");
  var logoInput = logo.querySelector("[data-logo-input]");
  var logoPick = logo.querySelector("[data-logo-pick]");
  var logoRemove = logo.querySelector("[data-logo-remove]");
  var logoError = logo.querySelector("[data-logo-error]");
  var FAILED = "Не сохранилось. Попробуйте ещё раз.";
  var SAVED = "Сохранено. Гости увидят новый вид, когда снова откроют чат.";

  function show(element, text) {
    element.textContent = text;
    element.hidden = !text;
  }

  function choice() {
    return {
      palette: form.querySelector("input[name=palette]:checked").value,
      pattern: form.querySelector("input[name=pattern]:checked").value,
    };
  }

  function refreshPreview() {
    var picked = choice();
    document.querySelectorAll("iframe[data-preview-theme]").forEach(function (frame) {
      var query = new URLSearchParams({
        palette: picked.palette,
        pattern: picked.pattern,
        theme: frame.dataset.previewTheme,
      });
      frame.src = endpoint + "/preview?" + query.toString();
    });
  }

  async function send(path, options) {
    var response = await fetch(endpoint + path, options);
    var data = null;
    try { data = await response.json(); } catch (error) { /* не JSON */ }
    if (response.ok) return null;
    return (data && data.error && data.error.message) || FAILED;
  }

  form.addEventListener("change", function () {
    show(done, "");
    refreshPreview();
  });

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    saveButton.disabled = true;
    show(done, "");
    show(failed, "");
    try {
      var problem = await send("", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(choice()),
      });
      if (problem) show(failed, problem); else show(done, SAVED);
    } catch (error) {
      show(failed, FAILED);
    } finally {
      saveButton.disabled = false;
    }
  });

  async function logoAction(path, options) {
    logoPick.disabled = true;
    if (logoRemove) logoRemove.disabled = true;
    show(logoError, "");
    try {
      var problem = await send(path, options);
      if (!problem) { window.location.reload(); return; }
      show(logoError, problem);
    } catch (error) {
      show(logoError, FAILED);
    }
    logoPick.disabled = false;
    if (logoRemove) logoRemove.disabled = false;
  }

  logoPick.addEventListener("click", function () { logoInput.click(); });

  logoInput.addEventListener("change", function () {
    var file = logoInput.files && logoInput.files[0];
    if (!file) return;
    var body = new FormData();
    body.append("logo", file, file.name);
    logoInput.value = "";
    logoAction("/logo", { method: "POST", body: body });
  });

  if (logoRemove) {
    logoRemove.addEventListener("click", function () {
      logoAction("/logo/remove", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    });
  }
})();
