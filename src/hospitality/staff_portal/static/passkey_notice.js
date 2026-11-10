/* Плашка «Привязано устройство для подписи» (spec 0077 §4, §9; layout.html,
 * passkey_notice_api.py). «Это я» — POST ack и плашка уходит; «Это не я» открывает поле
 * пароля, POST disown: отвязано — текст «попросите новый пароль», кнопок больше нет.
 * Ответ `ok: false` — плашку закрыли из другого входа раньше: тоже текстом. Тексты —
 * data-msg-* у [data-passkey-notices]; всё — textContent, разметки в JS нет.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-passkey-notices]");
  if (!root) return;

  async function post(url, body) {
    var response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    var data = null;
    try { data = await response.json(); } catch (error) { /* не конверт */ }
    if (!response.ok) {
      var envelope = (data && data.error) || {};
      var failure = new Error(envelope.code || String(response.status));
      failure.code = envelope.code;
      throw failure;
    }
    return data || {};
  }

  function message(error) {
    if (error.code === "ERR-AUTH-017") return root.dataset.msgPassword;
    if (error.code === "ERR-AUTH-006") return root.dataset.msgAttempts;
    return root.dataset.msgFailed;
  }

  root.querySelectorAll("[data-passkey-notice]").forEach(function (notice) {
    var buttons = notice.querySelector("[data-passkey-buttons]");
    var form = notice.querySelector("[data-passkey-disown]");
    var status = notice.querySelector("[data-passkey-status]");

    function show(text, final) {
      status.textContent = text;
      status.hidden = !text;
      if (final) {
        buttons.hidden = true;
        form.hidden = true;
      }
    }

    notice.querySelector("[data-passkey-ack]").addEventListener("click", async function () {
      try {
        var result = await post(notice.dataset.ackUrl);
        if (result.ok) notice.remove();
        else show(root.dataset.msgGone, true);
      } catch (error) {
        show(message(error), false);
      }
    });

    notice.querySelector("[data-passkey-disown-open]").addEventListener("click", function () {
      form.hidden = false;
      form.querySelector("input").focus();
    });

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      var input = form.querySelector("input");
      var submit = form.querySelector("button");
      submit.disabled = true;
      try {
        var result = await post(notice.dataset.disownUrl, { password: input.value });
        show(result.ok ? root.dataset.msgDisowned : root.dataset.msgGone, true);
      } catch (error) {
        show(message(error), false);
      } finally {
        input.value = "";
        submit.disabled = false;
      }
    });
  });
})();
