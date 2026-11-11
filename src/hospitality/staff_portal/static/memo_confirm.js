/* Окно подтверждения действия под именем (spec 0077 §4, §9; templates/_memo_confirm.html,
 * memos_actions_api.py): window.NecturnMemoConfirm.run(memoId, intent) → Promise<статус
 * записи | null>. null — человек закрыл окно, ничего не сделано.
 *
 *   POST {api}/{memoId}/begin {intent} → {challenge_id, options}; options — параметры
 *     navigator.credentials.get или null (ключей нет — только пароль);
 *   «Подтвердить на устройстве» → NecturnPasskey.get (static/passkey.js) → POST complete
 *     {intent, challenge_id, credential};
 *   «Ввести пароль кабинета» → POST complete {intent, challenge_id, password}.
 * Вызов одноразовый: после отказа ключа или устаревшего окна страница берёт новый
 * (begin ещё раз), а неверный пароль вызов не тратит. Тексты — data-msg-*; всё —
 * textContent, разметки в JS нет.
 */
(function () {
  "use strict";

  var dialog = document.querySelector("[data-memo-confirm]");
  if (!dialog) return;
  var msg = dialog.dataset;
  var api = msg.api;
  var deviceButton = dialog.querySelector("[data-confirm-device]");
  var passwordToggle = dialog.querySelector("[data-confirm-show-password]");
  var passwordBox = dialog.querySelector("[data-confirm-password-box]");
  var passwordInput = dialog.querySelector("[data-confirm-password]");
  var status = dialog.querySelector("[data-confirm-status]");
  var sampleLink = dialog.querySelector("[data-confirm-sample-link]");
  var form = dialog.querySelector("[data-confirm-form]");
  var current = null; // {memoId, intent, challenge, options, resolve}

  async function post(path, body) {
    var response = await fetch(api + path, {
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
      failure.reason = envelope.details && envelope.details[0] ? envelope.details[0].reason : null;
      throw failure;
    }
    return data || {};
  }

  function show(text) {
    status.textContent = text || "";
    status.hidden = !text;
  }

  function refusal(error) {
    if (error.code === "ERR-AUTH-017" && error.reason === "password") return msg.msgPassword;
    if (error.code === "ERR-AUTH-017" && error.reason === "expired") return msg.msgExpired;
    if (error.code === "ERR-AUTH-017") return msg.msgConfirmFailed;
    if (error.code === "ERR-AUTH-006") return msg.msgTooManyAttempts;
    if (error.code === "ERR-MEMO-003") return msg.msgNoSample;
    if (error.code === "ERR-MEMO-002") return msg.msgChanged;
    if (error.code === "ERR-PLATFORM-002" && error.reason === "comment_required") {
      return msg.msgCommentRequired;
    }
    return msg.msgFailed;
  }

  function busy(on) {
    deviceButton.disabled = on;
    passwordToggle.disabled = on;
    form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = on; });
  }

  /* Новый вызов: первый раз и после отказа, который вызов израсходовал. */
  async function begin() {
    var started = await post("/" + current.memoId + "/begin", { intent: current.intent });
    current.challenge = started.challenge_id;
    current.options = started.options;
    deviceButton.hidden = true;
    if (started.options && (await window.NecturnPasskey.available())) deviceButton.hidden = false;
    else showPassword();
  }

  function showPassword() {
    passwordBox.hidden = false;
    passwordToggle.hidden = true;
    passwordInput.focus();
  }

  function finish(result) {
    var done = current;
    current = null;
    passwordInput.value = "";
    if (dialog.open) dialog.close();
    if (done) done.resolve(result);
  }

  async function complete(answer) {
    busy(true);
    show("");
    try {
      var body = Object.assign({ intent: current.intent, challenge_id: current.challenge }, answer);
      var done = await post("/" + current.memoId + "/complete", body);
      finish(done.status);
    } catch (error) {
      show(refusal(error));
      sampleLink.hidden = error.code !== "ERR-MEMO-003";
      var spent = error.code === "ERR-AUTH-017" && error.reason !== "password";
      if (spent && current) {
        try { await begin(); } catch (again) { show(refusal(again)); }
      }
    } finally {
      passwordInput.value = "";
      busy(false);
    }
  }

  deviceButton.addEventListener("click", async function () {
    var credential;
    try {
      credential = await window.NecturnPasskey.get(current.options);
    } catch (error) {
      // Отказ на устройстве или браузера: вызов цел, можно снова или паролем.
      show(msg.msgConfirmFailed);
      return;
    }
    await complete({ credential: credential });
  });
  passwordToggle.addEventListener("click", showPassword);
  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!passwordInput.value) return;
    await complete({ password: passwordInput.value });
  });
  dialog.querySelector("[data-confirm-cancel]").addEventListener("click", function () {
    finish(null);
  });
  dialog.addEventListener("cancel", function () { finish(null); });

  async function run(memoId, intent) {
    if (current) finish(null);
    return new Promise(function (resolve) {
      current = { memoId: memoId, intent: intent, resolve: resolve };
      show("");
      sampleLink.hidden = true;
      passwordBox.hidden = true;
      passwordToggle.hidden = false;
      deviceButton.hidden = true;
      dialog.showModal();
      begin().catch(function (error) {
        show(refusal(error));
        sampleLink.hidden = error.code !== "ERR-MEMO-003";
        deviceButton.hidden = true;
        passwordToggle.hidden = true;
      });
    });
  }

  window.NecturnMemoConfirm = { run: run };
})();
