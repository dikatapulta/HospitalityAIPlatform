/* Поле «Фото» формы товара (spec 0038 §6 PR 7, issue #418).
 *
 * Отдельный файл от catalog.js (R-3): выбор и перетаскивание файла,
 * предпросмотр до загрузки и сама загрузка. Форму открывает и сохраняет
 * catalog.js, ему этот файл отдаёт window.CatalogPhoto:
 *   reset(url)        — форма открыта: фото товара (адрес панели) или «нет фото»;
 *   pending()         — есть ли что отправить при «Сохранить»;
 *   commit(itemId)    — отправить выбранное: тот же ответ, что send() в catalog.js;
 *   pick()            — «Добавить фото» из меню карточки: сразу выбор файла.
 * Фото сохраняется вместе с формой, а не в момент выбора: «Отмена» не меняет
 * ничего, а у нового товара id появляется только после «Добавить товар».
 *
 * CSRF-контракт загрузки (browser.require_upload_same_origin): тело
 * multipart/form-data из FormData, Origin браузер добавляет сам. Предпросмотр —
 * blob: (CSP страницы каталога разрешает его только картинкам); не показался
 * (HEIC на компьютере) — остаётся метка, файл всё равно проверит сервер.
 * Проверки здесь — только подсказка до отправки: решает сервер (spec 0047 §4).
 */
(function () {
  "use strict";

  var box = document.getElementById("catalog-photo");
  var input = document.getElementById("catalog-photo-file");
  var board = document.getElementById("catalog-board");
  var formError = document.getElementById("catalog-form-error");
  if (!box || !input || !board || !formError) return;
  var endpoint = board.dataset.endpoint;
  var preview = box.querySelector("[data-photo-preview]");
  var img = box.querySelector("[data-photo-img]");
  var note = box.querySelector("[data-photo-note]");
  var tools = box.querySelector("[data-photo-tools]");
  var drop = box.querySelector("[data-photo-drop]");

  var MAX_BYTES = 5 * 1024 * 1024;
  var TYPES = /^image\/(jpeg|png|webp)$/;
  var SAVED_NOTE = "у гостя — рамка 4:3 · до 960 px · без метаданных";
  var PENDING_NOTE = "загрузим при сохранении";

  /* url — фото товара на сервере; file — выбранный, ещё не отправленный;
   * removed — нажали «Убрать фото»; blob — адрес предпросмотра file. */
  var state = { url: null, file: null, removed: false, blob: null };

  function release() {
    if (state.blob) URL.revokeObjectURL(state.blob);
    state.blob = null;
  }

  function show(url, text) {
    preview.hidden = !url;
    tools.hidden = !url;
    drop.hidden = !!url;
    if (url) img.src = url;
    else img.removeAttribute("src");
    note.textContent = text;
  }

  function fail(text) {
    formError.textContent = text;
    formError.hidden = false;
  }

  function take(file) {
    if (!file) return;
    if (file.type && !TYPES.test(file.type)) return fail("Нужен JPEG, PNG или WebP.");
    if (file.size > MAX_BYTES) {
      return fail("Фото больше 5 МБ — сожмите его на телефоне и загрузите снова.");
    }
    formError.hidden = true;
    release();
    state.file = file;
    state.removed = false;
    state.blob = URL.createObjectURL(file);
    show(state.blob, PENDING_NOTE);
  }

  function reset(url) {
    release();
    state = { url: url || null, file: null, removed: false, blob: null };
    input.value = "";
    show(state.url, SAVED_NOTE);
  }

  async function commit(itemId) {
    var options;
    var path = "/items/" + itemId + "/photo";
    if (state.file) {
      var body = new FormData();
      body.append("photo", state.file, state.file.name || "photo");
      options = { method: "POST", body: body };
    } else if (state.removed) {
      path += "/remove";
      options = { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" };
    } else {
      return { ok: true, status: 200, code: "", data: null };
    }
    try {
      var response = await fetch(endpoint + path, options);
      var data = null;
      try { data = await response.json(); } catch (error) { /* не JSON */ }
      var code = !response.ok && data && data.error ? data.error.code : "";
      return { ok: response.ok, status: response.status, code: code, data: data };
    } catch (error) {
      return { ok: false, status: 0, code: "network", data: null };
    }
  }

  box.addEventListener("click", function (event) {
    var button = event.target.closest("[data-act]");
    if (!button) return;
    if (button.dataset.act === "photo-pick") input.click();
    if (button.dataset.act === "photo-remove") {
      release();
      state.file = null;
      state.removed = !!state.url;
      input.value = "";
      show(null, "");
    }
  });

  input.addEventListener("change", function () {
    take(input.files && input.files[0]);
  });

  /* Предпросмотр не показался (формат, который браузер не рисует, или фото
   * на сервере недоступно) — серая рамка с меткой вместо битой картинки. */
  img.addEventListener("error", function () { img.removeAttribute("src"); });

  ["dragenter", "dragover"].forEach(function (name) {
    drop.addEventListener(name, function (event) {
      event.preventDefault();
      drop.classList.add("is-over");
    });
  });
  ["dragleave", "drop"].forEach(function (name) {
    drop.addEventListener(name, function (event) {
      event.preventDefault();
      drop.classList.remove("is-over");
    });
  });
  drop.addEventListener("drop", function (event) {
    var files = event.dataTransfer && event.dataTransfer.files;
    take(files && files[0]);
  });

  window.CatalogPhoto = {
    reset: reset,
    pending: function () { return !!state.file || state.removed; },
    commit: commit,
    pick: function () { input.click(); }
  };
})();
