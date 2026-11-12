/* «Оформить» в черновике записки (spec 0077 §7, §9; PR 6; templates/memo_draft.html,
 * memos_format_api.py). Текст поля и выбранный шаблон → POST …/api/memos/format →
 * оформленный текст встаёт в поле, над ним плашка «Так оформил ИИ — проверьте»:
 * «Принять» оставляет его, «Вернуть мой текст» — прежний. Пока плашка открыта, «Отправить…»
 * не нажимается: сначала решает автор (P-9). Отказ — текст по коду ошибки шлюза из
 * data-msg-*; всё — textContent, разметки в JS нет.
 */
(function () {
  "use strict";

  var box = document.querySelector("[data-memo-format]");
  if (!box) return;
  var form = box.closest("[data-memo-draft]");
  var body = form.querySelector("#memo-body");
  var template = form.querySelector("[data-template]");
  var send = form.querySelector("[data-draft-send]");
  var run = box.querySelector("[data-format-run]");
  var note = form.querySelector("[data-format-note]");
  var status = form.querySelector("[data-format-status]");
  var msg = box.dataset;
  var label = run.textContent;
  var original = null;

  function show(text) {
    status.textContent = text || "";
    status.hidden = !text;
  }

  function refusal(code) {
    if (code === "ERR-AI-002") return msg.msgBudget;
    if (code === "ERR-AI-007") return msg.msgIncomplete;
    return msg.msgUnavailable;
  }

  function decided() {
    note.hidden = true;
    original = null;
    send.disabled = false;
    run.disabled = false;
  }

  run.addEventListener("click", async function () {
    var text = body.value.trim();
    show("");
    if (!text) { show(msg.msgEmpty); return; }
    run.disabled = true;
    run.textContent = msg.labelBusy;
    var code = null;
    try {
      var response = await fetch(msg.api, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text, template_key: template.value || null })
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не конверт */ }
      if (response.ok && data && data.text) {
        original = body.value;
        body.value = data.text;
        note.hidden = false;
        send.disabled = true;
        run.textContent = label;
        return;
      }
      code = data && data.error ? data.error.code : null;
    } catch (error) { /* сеть: тот же текст, что у молчащего ИИ */ }
    show(refusal(code));
    run.textContent = label;
    run.disabled = false;
  });

  note.querySelector("[data-format-accept]").addEventListener("click", decided);
  note.querySelector("[data-format-undo]").addEventListener("click", function () {
    if (original !== null) body.value = original;
    decided();
  });
})();
