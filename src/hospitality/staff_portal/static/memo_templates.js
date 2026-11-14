/* «Шаблоны записок» менеджера (spec 0077 §7; PR 6; templates/memo_templates.html,
 * memos_templates_api.py). Строки — название и текст; «Удалить» убирает строку с экрана,
 * «Добавить шаблон» — новая строка из <template>, не больше data-max. «Сохранить» — POST
 * списка целиком; ответ — ключи в порядке строк: новые строки запоминают свой ключ, и
 * повторное «Сохранить» не плодит копий. Тексты — data-msg-*; всё — textContent.
 */
(function () {
  "use strict";

  var form = document.querySelector("[data-memo-templates]");
  if (!form) return;
  var msg = form.dataset;
  var list = form.querySelector("[data-template-list]");
  var blank = form.querySelector("[data-template-blank]");
  var add = form.querySelector("[data-template-add]");
  var status = form.querySelector("[data-templates-status]");
  var save = form.querySelector("button[type=submit]");
  var max = Number(msg.max);

  function show(text) {
    status.textContent = text || "";
    status.hidden = !text;
  }

  function items() {
    return Array.prototype.slice.call(list.querySelectorAll("[data-template-item]"));
  }

  function sync() { add.disabled = items().length >= max; }

  function bind(item) {
    item.querySelector("[data-template-delete]").addEventListener("click", function () {
      item.remove();
      show("");
      sync();
    });
  }

  items().forEach(bind);
  sync();

  add.addEventListener("click", function () {
    if (items().length >= max) { show(msg.msgLimit); return; }
    var item = blank.content.firstElementChild.cloneNode(true);
    list.appendChild(item);
    bind(item);
    sync();
    show("");
    item.querySelector("[name=title]").focus();
  });

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var rows = items();
    var templates = rows.map(function (item) {
      return {
        key: item.dataset.key || null,
        title: item.querySelector("[name=title]").value.trim(),
        body: item.querySelector("[name=body]").value.trim()
      };
    });
    if (templates.some(function (row) { return !row.title || !row.body; })) {
      show(msg.msgIncomplete);
      return;
    }
    save.disabled = true;
    show("");
    try {
      var response = await fetch(msg.api, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ templates: templates })
      });
      var data = response.ok ? await response.json() : null;
      if (data && data.keys) {
        data.keys.forEach(function (key, index) { if (rows[index]) rows[index].dataset.key = key; });
        show(msg.msgSaved);
      } else {
        show(msg.msgFailed);
      }
    } catch (error) {
      show(msg.msgFailed);
    }
    save.disabled = false;
  });
})();
