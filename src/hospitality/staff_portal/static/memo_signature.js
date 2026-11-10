/* «Служебные записки» → «Моя подпись» (spec 0077 §4; templates/memo_signature.html,
 * memo_signature_api.py). Три действия:
 *   привязка — пароль → POST bind/begin → ключ устройства (NecturnPasskey.create, static/
 *     passkey.js) → POST bind/finish; браузер без встроенного ключа видит текст «подписывать
 *     можно паролем», а не форму;
 *   отвязка своего устройства — пароль → POST devices/{id}/revoke;
 *   образец подписи — поле 5:2 (x 0…1000, y 0…400, как рисует _memo_signature_svg.html):
 *     штрихи пальцем или мышью → POST sample.
 * Успех — перезагрузка страницы, текст успеха переживает её через sessionStorage. Тексты —
 * data-msg-* у [data-memo-signature]; всё — textContent, разметки в JS нет.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-memo-signature]");
  if (!root) return;
  var api = root.dataset.api;
  var msg = root.dataset;
  var FLASH = "memo-signature-flash";
  // Пределы модуля memos (schemas.MAX_STROKES, MAX_STROKE_POINTS).
  var MAX_STROKES = 64;
  var MAX_POINTS = 4000;
  var WIDTH = 1000;
  var HEIGHT = 400;

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

  function show(node, text) {
    node.textContent = text || "";
    node.hidden = !text;
  }

  /* Текст успеха переживает перезагрузку: где показать ("bind" или "sample") и что. */
  function reloadWith(box, text) {
    try {
      window.sessionStorage.setItem(FLASH, JSON.stringify({ box: box, text: text }));
    } catch (error) { /* приватный режим */ }
    window.location.reload();
  }

  function refusal(error, fallback) {
    if (error.code === "ERR-AUTH-017" && error.reason === "password") return msg.msgPassword;
    if (error.code === "ERR-AUTH-006") return msg.msgTooManyAttempts;
    if (error.code === "ERR-AUTH-018") {
      if (error.reason === "limit") return msg.msgLimit;
      if (error.reason === "already_bound") return msg.msgAlreadyBound;
      if (error.reason === "not_remembered") return msg.msgNotRemembered;
      if (error.reason === "platform_admin") return msg.msgPlatformAdmin;
    }
    return fallback;
  }

  var bindStatus = root.querySelector("[data-bind-status]");
  var sampleStatus = root.querySelector("[data-sample-status]");
  try {
    var flash = JSON.parse(window.sessionStorage.getItem(FLASH) || "null");
    window.sessionStorage.removeItem(FLASH);
    if (flash) show(flash.box === "sample" ? sampleStatus : bindStatus, flash.text);
  } catch (error) { /* приватный режим или чужое значение */ }

  /* ---------- Привязать это устройство ---------- */

  var bindForm = root.querySelector("[data-bind-form]");
  if (bindForm) {
    window.NecturnPasskey.available().then(function (ok) {
      if (ok) bindForm.hidden = false;
      else root.querySelector("[data-no-key]").hidden = false;
    });
    bindForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var input = bindForm.querySelector("input");
      var submit = bindForm.querySelector("button");
      submit.disabled = true;
      show(bindStatus, "");
      try {
        var begun = await post("/bind/begin", { password: input.value });
        var credential;
        try {
          credential = await window.NecturnPasskey.create(begun.options);
        } catch (error) {
          show(bindStatus, msg.msgBindFailed);
          return;
        }
        var done = await post("/bind/finish", { credential: credential });
        reloadWith("bind", msg.msgBound.replace("{label}", done.device_label));
      } catch (error) {
        show(bindStatus, refusal(error, msg.msgBindFailed));
      } finally {
        input.value = "";
        submit.disabled = false;
      }
    });
  }

  /* ---------- Отвязать своё устройство ---------- */

  var devicesStatus = root.querySelector("[data-devices-status]");
  root.querySelectorAll("[data-revoke-form]").forEach(function (form) {
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      var input = form.querySelector("input");
      var submit = form.querySelector("button");
      submit.disabled = true;
      try {
        await post("/devices/" + form.dataset.passkeyId + "/revoke", { password: input.value });
        window.location.reload();
      } catch (error) {
        show(devicesStatus, refusal(error, msg.msgFailed));
      } finally {
        input.value = "";
        submit.disabled = false;
      }
    });
  });

  /* ---------- Образец подписи ---------- */

  var pad = root.querySelector("[data-pad]");
  var padBox = root.querySelector("[data-sample-pad]");
  var redo = root.querySelector("[data-sample-redo]");
  var context = pad.getContext("2d");
  var strokes = [];
  var current = null;

  if (redo) {
    redo.addEventListener("click", function () {
      root.querySelector("[data-sample-current]").hidden = true;
      padBox.hidden = false;
    });
  }

  function point(event) {
    var box = pad.getBoundingClientRect();
    var x = Math.round(((event.clientX - box.left) * WIDTH) / box.width);
    var y = Math.round(((event.clientY - box.top) * HEIGHT) / box.height);
    return [Math.min(WIDTH, Math.max(0, x)), Math.min(HEIGHT, Math.max(0, y))];
  }

  function draw() {
    context.clearRect(0, 0, WIDTH, HEIGHT);
    context.lineWidth = 6;
    context.lineCap = "round";
    context.lineJoin = "round";
    context.strokeStyle = window.getComputedStyle(pad).color;
    strokes.forEach(function (stroke) {
      context.beginPath();
      context.moveTo(stroke[0][0], stroke[0][1]);
      stroke.forEach(function (p) { context.lineTo(p[0], p[1]); });
      if (stroke.length === 1) context.lineTo(stroke[0][0] + 0.1, stroke[0][1]);
      context.stroke();
    });
  }

  pad.addEventListener("pointerdown", function (event) {
    if (strokes.length >= MAX_STROKES) return;
    event.preventDefault();
    pad.setPointerCapture(event.pointerId);
    current = [point(event)];
    strokes.push(current);
    show(sampleStatus, "");
    draw();
  });
  pad.addEventListener("pointermove", function (event) {
    if (!current) return;
    var next = point(event);
    var last = current[current.length - 1];
    // Точки ближе 3 единиц рисунка не нужны: поле в 1000 единиц, палец толще.
    if (Math.abs(next[0] - last[0]) + Math.abs(next[1] - last[1]) < 3) return;
    current.push(next);
    draw();
  });
  ["pointerup", "pointercancel"].forEach(function (name) {
    pad.addEventListener(name, function () { current = null; });
  });

  root.querySelector("[data-pad-clear]").addEventListener("click", function () {
    strokes = [];
    current = null;
    draw();
  });

  /* Длинная подпись прореживается равномерно — под предел точек модуля; конец штриха
   * остаётся всегда, запас под концы — по две точки на штрих. */
  function thinned() {
    var total = strokes.reduce(function (sum, stroke) { return sum + stroke.length; }, 0);
    var step = Math.ceil(total / (MAX_POINTS - 2 * MAX_STROKES));
    if (step <= 1) return strokes;
    return strokes.map(function (stroke) {
      var kept = stroke.filter(function (p, index) { return index % step === 0; });
      if (kept[kept.length - 1] !== stroke[stroke.length - 1]) kept.push(stroke[stroke.length - 1]);
      return kept;
    });
  }

  root.querySelector("[data-pad-save]").addEventListener("click", async function (event) {
    if (!strokes.length) {
      show(sampleStatus, msg.msgSampleEmpty);
      return;
    }
    var button = event.currentTarget;
    button.disabled = true;
    try {
      await post("/sample", { strokes: thinned() });
      reloadWith("sample", msg.msgSampleSaved);
    } catch (error) {
      show(sampleStatus, msg.msgFailed);
      button.disabled = false;
    }
  });
})();
