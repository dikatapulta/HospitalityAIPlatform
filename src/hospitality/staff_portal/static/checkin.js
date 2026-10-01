/* Страница заселения кабинета (spec 0033 §6, PR E серии #48).
 *
 * Ванильный JS карточки Stay (канон queue.js): JSON-действия по CSRF-контракту
 * (Content-Type: application/json + Origin от fetch), поллинг счётчика привязок
 * каждые 3 с, печать листка гостю (window.print, макет — @media print в
 * styles.css). Разметку и тексты JS не сочиняет: карточка — Jinja
 * (_stay_card.html), готовые подсказки лежат в её data-атрибутах; единственный
 * innerHTML — вставка ГОТОВОГО серверного SVG-QR из ответа bind-link/reissue-code
 * на экран и на листок (сервер — единственный автор этой разметки). QR действует
 * до выезда, поэтому таймера у него нет.
 */
(function () {
  "use strict";

  var BINDINGS_POLL_MS = 3000;

  var card = document.querySelector(".stay-card");
  if (!card) return;
  var endpoint = card.dataset.endpoint + "/" + card.dataset.stayId;
  var status = card.querySelector("[data-card-status]");
  var qrBox = card.querySelector("[data-qr]");
  var qrHint = card.querySelector("[data-qr-hint]");
  var codeEl = card.querySelector("[data-code]");
  var codeHint = card.querySelector("[data-code-hint]");
  var showQrButton = card.querySelector("[data-action=bind-link]");
  var checkOutEl = card.querySelector("[data-check-out]");
  var bindingsLine = card.querySelector("[data-bindings]");
  var bindingsCount = card.querySelector("[data-bindings-count]");
  var moveForm = card.querySelector("[data-move-form]");
  var printQr = card.querySelector("[data-print-qr]");
  var printCode = card.querySelector("[data-print-code]");

  /* Дружелюбные тексты по кодам каталога ошибок (R-8). */
  var MESSAGES = {
    "ERR-GUESTS-001": "Проживание уже закрыто — обновите страницу.",
    "ERR-GUESTS-002": "Комната занята — выберите другую.",
    "ERR-GUESTS-003": "Код уже перевыпускается — попробуйте ещё раз.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию.",
    "ERR-AUTH-010": "Слишком много QR подряд — подождите минуту."
  };

  function showStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  async function post(action, payload) {
    var response = await fetch(endpoint + "/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
    var data = null;
    try { data = await response.json(); } catch (error) { /* не конверт */ }
    if (!response.ok) {
      var code = data && data.error && data.error.code;
      throw new Error(MESSAGES[code] || "Не получилось (" + (code || response.status) + "). Попробуйте ещё раз.");
    }
    return data;
  }

  /* Свежие QR (и код, если перевыпущен) — на экран и сразу на листок: листок
   * всегда равен экрану, поэтому и печать через Ctrl+P не уносит гостю QR,
   * погашенный перевыпуском. «Показать QR» больше не нужна — QR живёт до выезда. */
  function showAccess(qrSvg, accessCode) {
    qrBox.innerHTML = qrSvg;
    printQr.innerHTML = qrSvg;
    qrHint.textContent = qrHint.dataset.ready;
    showQrButton.hidden = true;
    if (accessCode) {
      codeEl.textContent = accessCode;
      printCode.textContent = accessCode;
      codeHint.textContent = codeHint.dataset.ready;
    }
  }

  /* Индикатор «гость подключился»: рост счётчика после открытия карточки. */
  var initialBindings = parseInt(card.dataset.bindingsInitial, 10) || 0;
  setInterval(async function () {
    if (document.hidden) return;
    try {
      var response = await fetch(endpoint + "/bindings", { cache: "no-store" });
      if (response.redirected) { window.location.href = response.url; return; }
      if (!response.ok) return;
      var data = await response.json();
      bindingsCount.textContent = data.count;
      if (data.count > initialBindings) {
        bindingsLine.classList.add("ok");
        bindingsLine.textContent = "Гость подключился ✓ (подключений: " + data.count + ")";
      }
    } catch (error) {
      /* Нет сети — следующий тик попробует снова. */
    }
  }, BINDINGS_POLL_MS);

  async function run(button, action) {
    var buttons = card.querySelectorAll("button");
    buttons.forEach(function (b) { b.disabled = true; });
    showStatus("");
    try {
      if (action === "bind-link") {
        var link = await post("bind-link");
        showAccess(link.qr_svg, null);
      } else if (action === "print") {
        /* QR на экране нет (карточку открыли поиском) — листок без QR гостю
         * бесполезен: выпустить его и только потом печатать. */
        var issued = await post("bind-link");
        showAccess(issued.qr_svg, null);
        window.print();
      } else if (action === "reissue-code") {
        var reissued = await post("reissue-code");
        showAccess(reissued.qr_svg, reissued.access_code);
      } else if (action === "extend") {
        var extended = await post("extend", { nights: parseInt(button.dataset.nights, 10) });
        checkOutEl.textContent = extended.check_out_local;
      } else if (action === "checkout") {
        await post("checkout");
        window.location.href = window.location.pathname;
        return;
      }
    } catch (error) {
      showStatus(error.message);
    } finally {
      buttons.forEach(function (b) { b.disabled = false; });
    }
  }

  card.addEventListener("click", function (event) {
    if (event.target.closest("[data-move-cancel]")) {
      moveForm.hidden = true;
      return;
    }
    var button = event.target.closest("[data-action]");
    if (!button) return;
    var action = button.dataset.action;
    if (action === "show-move") {
      moveForm.hidden = false;
      moveForm.querySelector("input[name=room]").focus();
      return;
    }
    if (action === "print" && qrBox.querySelector("svg")) {
      /* Листок печатает браузер (@media print в styles.css); QR и код на нём
       * уже равны экрану (showAccess). */
      window.print();
      return;
    }
    if (action === "checkout" && !window.confirm("Выселить гостя? Доступ к чату сразу погаснет.")) {
      return;
    }
    if (action === "reissue-code" &&
        !window.confirm("Перевыпустить код и QR? Старый листок перестанет работать.")) {
      return;
    }
    run(button, action);
  });

  moveForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    var room = moveForm.querySelector("input[name=room]").value.trim();
    if (!room) {
      showStatus("Укажите новую комнату.");
      return;
    }
    try {
      var moved = await post("move", { room_number: room });
      window.location.href = "?room=" + encodeURIComponent(moved.room_number);
    } catch (error) {
      showStatus(error.message);
    }
  });
})();
