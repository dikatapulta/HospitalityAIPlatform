/* Черновик АВР на странице «Документы для АВР» (spec 0086 §4; issue #953).
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Разметка формы живёт в Jinja (act_requests.html, контракт селекторов — там).
 * Свои атрибуты data-act-*, а не data-action: обработчик act_requests.js не
 * узнаёт эти кнопки и не открывает по ним форму примечания.
 *
 * Стоимость и «Итого» здесь — подсказка бухгалтеру; в акт ложатся суммы, которые
 * посчитал сервер (documents.acts, Р-3). Счёт целыми — BigInt в тиынах и тысячных
 * долях количества, half-up, как на сервере: дробные числа JS теряли бы тиыны.
 *
 * CSRF-контракт JSON-действий (докстринг router.py): POST несёт
 * Content-Type: application/json — Origin браузер добавляет сам.
 */
(function () {
  "use strict";

  var table = document.getElementById("act-table");
  var status = document.getElementById("act-status");
  if (!table || !status) return;
  var endpoint = table.dataset.endpoint;
  var maxLines = Number(table.dataset.actMaxLines) || 20;

  var MESSAGES = {
    "ERR-DOCUMENTS-007": "Реквизиты изменились после проверки — нажмите «Проверено» и " +
      "сформируйте акт заново",
    "ERR-DOCUMENTS-008": "Реквизиты отеля для АВР не заданы — их вносит администратор платформы",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-AUTH-003": "Нет доступа к этому действию."
  };
  var INVALID = "Проверьте поля акта — номер, дату и строки.";

  function showStatus(text) {
    status.textContent = text;
    status.className = "error";
    status.hidden = !text;
  }

  /* «22 500,5» → «22500.5»: пробелы — разряды, запятая — дробная часть. */
  function plain(raw) {
    return raw.replace(/[\s  ]/g, "").replace(",", ".");
  }

  /* Число из поля → целое в долях 10^places (BigInt) или null: знаков после
   * запятой не больше places — столько же принимает сервер. */
  function scaled(raw, places) {
    var text = plain(raw);
    var match = new RegExp("^(\\d+)(?:\\.(\\d{1," + places + "}))?$").exec(text);
    if (!match) return null;
    var fraction = (match[2] || "").padEnd(places, "0");
    return BigInt(match[1] + fraction);
  }

  /* Тиыны → «45 000,00»: неразрывные пробелы и запятая, как в списке актов. */
  function money(tiyn) {
    var text = tiyn.toString().padStart(3, "0");
    var whole = text.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    return whole + "," + text.slice(-2);
  }

  /* Стоимость строки в тиынах: количество (×1000) × цена (×100), до тиына half-up. */
  function lineCost(line) {
    var quantity = scaled(line.querySelector("[name=quantity]").value, 3);
    var price = scaled(line.querySelector("[name=price]").value, 2);
    if (quantity === null || price === null || quantity === 0n) return null;
    return (quantity * price + 500n) / 1000n;
  }

  function recalc(form) {
    var total = 0n;
    var lines = form.querySelectorAll("[data-act-line]");
    lines.forEach(function (line) {
      var cost = lineCost(line);
      line.querySelector("[data-act-cost]").textContent = cost === null ? "—" : money(cost);
      if (cost !== null) total += cost;
      line.querySelector("[data-act-remove-line]").hidden = lines.length === 1;
    });
    form.querySelector("[data-act-total]").textContent = money(total);
    form.querySelector("[data-act-add-line]").hidden = lines.length >= maxLines;
  }

  function addLine(form) {
    var first = form.querySelector("[data-act-line]");
    var line = first.cloneNode(true);
    line.querySelectorAll("input").forEach(function (input) { input.value = ""; });
    form.querySelector("[data-act-lines]").appendChild(line);
    recalc(form);
    line.querySelector("[name=service]").focus();
  }

  function value(scope, name) {
    return scope.querySelector("[name=" + name + "]").value.trim();
  }

  /* Тело запроса или текст ошибки: обязательное и формат чисел — до сервера. */
  function collect(form) {
    var body = {
      number: value(form, "number"),
      composed_on: value(form, "composed_on"),
      contract: value(form, "contract"),
      lines: []
    };
    if (!body.number || !body.composed_on) return "Впишите номер акта и дату составления.";
    var lines = form.querySelectorAll("[data-act-line]");
    for (var index = 0; index < lines.length; index += 1) {
      var line = lines[index];
      var item = {
        service: value(line, "service"),
        service_date: value(line, "service_date"),
        unit: value(line, "unit"),
        quantity: value(line, "quantity"),
        price: value(line, "price")
      };
      var label = "Строка " + (index + 1) + ": ";
      if (!item.service || !item.unit || !item.quantity || !item.price) {
        return label + "впишите услугу, единицу, количество и цену.";
      }
      var quantity = scaled(item.quantity, 3);
      var price = scaled(item.price, 2);
      if (quantity === null || quantity === 0n || price === null) {
        return label + "количество — число больше нуля, до трёх знаков после запятой; " +
          "цена — до двух.";
      }
      item.quantity = plain(item.quantity);
      item.price = plain(item.price);
      body.lines.push(item);
    }
    return body;
  }

  async function submit(form) {
    var body = collect(form);
    if (typeof body === "string") {
      showStatus(body);
      return;
    }
    var group = form.closest("[data-act-id]");
    var buttons = form.querySelectorAll("button");
    buttons.forEach(function (button) { button.disabled = true; });
    showStatus("");
    var code;
    try {
      var response = await fetch(endpoint + "/act-requests/" + group.dataset.actId + "/acts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      if (response.ok) {
        window.location.reload();
        return;
      }
      try { code = (await response.json()).error.code || String(response.status); }
      catch (error) { code = String(response.status); }
      if (response.status === 422 && !MESSAGES[code]) code = "invalid";
    } catch (error) {
      code = "network";
    }
    /* Запрос обезличен или пропал — строка устарела, страница покажет, как есть. */
    if (code === "ERR-DOCUMENTS-004") {
      window.location.reload();
      return;
    }
    showStatus(code === "network" ? "Нет связи — попробуйте ещё раз."
      : code === "invalid" ? INVALID
        : MESSAGES[code] || "Не получилось (" + code + "). Попробуйте ещё раз.");
    buttons.forEach(function (button) { button.disabled = false; });
  }

  table.addEventListener("click", function (event) {
    var open = event.target.closest("[data-act-open]");
    if (open) {
      var form = open.closest("[data-act-id]").querySelector("[data-act-form]");
      form.hidden = false;
      open.hidden = true;
      recalc(form);
      form.querySelector("[name=number]").focus();
      return;
    }
    var target = event.target.closest("[data-act-cancel], [data-act-add-line], [data-act-remove-line]");
    if (!target) return;
    var actForm = target.closest("[data-act-form]");
    if (target.hasAttribute("data-act-cancel")) {
      actForm.hidden = true;
      actForm.closest("[data-act-id]").querySelector("[data-act-open]").hidden = false;
      showStatus("");
    } else if (target.hasAttribute("data-act-add-line")) {
      addLine(actForm);
    } else {
      target.closest("[data-act-line]").remove();
      recalc(actForm);
    }
  });

  table.addEventListener("input", function (event) {
    var form = event.target.closest("[data-act-form]");
    if (form) recalc(form);
  });

  table.addEventListener("submit", function (event) {
    var form = event.target.closest("[data-act-form]");
    if (!form) return;
    event.preventDefault();
    submit(form);
  });
})();
