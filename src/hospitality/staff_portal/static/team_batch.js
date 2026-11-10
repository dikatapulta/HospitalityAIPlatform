/* «Пригласить списком» на странице «Сотрудники» (spec 0072 §4, тексты — §8).
 *
 * Имена — столбцом в поле; «Составить список» строит таблицу «Имя — Логин»:
 * логин подсказывает Team.suggestLogin (team_common.js), совпадения в списке и с
 * логинами, уже стоящими на странице, получают цифру (IVAA, IVAA2). Таблица
 * правится, «Выпустить приглашения» шлёт весь список. Сервер проверяет его
 * целиком: одна плохая строка не выпускает ни одной (ERR-AUTH-012, номера строк
 * в `details`) — текст встаёт у своей строки.
 *
 * Ответ — лист для печати: карточки из <template data-sheet-card> (team.html),
 * тексты — textContent, QR — SVG сервера (segno) через innerHTML: ввода менеджера
 * в нём нет. Ссылки видны только сейчас — лист живёт до перезагрузки страницы.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-endpoint]");
  var panel = root && root.querySelector("[data-batch-panel]");
  if (!panel) return;
  var Team = window.Team;
  var url = root.dataset.endpoint + "/invites/batch";
  var managerRole = root.dataset.managerRole;
  var openButton = root.querySelector("[data-batch-open]");
  var form = panel.querySelector("[data-batch-form]");
  var namesInput = form.querySelector("textarea[name=names]");
  var status = form.querySelector("[data-batch-status]");
  var tableBox = form.querySelector("[data-batch-table]");
  var rowsBox = form.querySelector("[data-batch-rows]");
  var countEl = form.querySelector("[data-batch-count]");
  var rowTemplate = panel.querySelector("template[data-batch-row]");
  var sheet = root.querySelector("[data-batch-sheet]");
  var MAX_ROWS = 100;
  var NO_NAMES_TEXT = "Впишите имена — по одному в строке.";
  var TOO_MANY_TEXT = "За раз — до 100 человек: разделите список на части.";
  var NO_NAME_TEXT = "Укажите имя сотрудника.";
  var DUPLICATE_TEXT = "Этот логин уже есть в списке выше.";
  var NOT_ISSUED_TEXT = "Приглашения не выпущены — поправьте отмеченные строки.";

  Team.bindRoleForm(form, managerRole);

  openButton.addEventListener("click", function () {
    var open = panel.hidden;
    panel.hidden = !open;
    openButton.setAttribute("aria-expanded", String(open));
    if (open) namesInput.focus();
  });

  function showStatus(text) {
    status.textContent = text;
    status.hidden = !text;
  }

  /* Логины, уже стоящие на странице (состав и ожидающие приглашения): подсказка
   * их обходит, чтобы выпуск не упёрся в «занят». Остальное знает сервер. */
  function knownLogins() {
    var used = {};
    root.querySelectorAll("[data-known-login]").forEach(function (element) {
      used[element.dataset.knownLogin] = true;
    });
    return used;
  }

  /* IVAA занят — IVAA2, IVAA3…, не длиннее 12 знаков. */
  function freeLogin(base, used) {
    if (!base || !used[base]) return base;
    for (var n = 2; ; n++) {
      var suffix = String(n);
      var candidate = base.slice(0, 12 - suffix.length) + suffix;
      if (!used[candidate]) return candidate;
    }
  }

  function tableRows() {
    return Array.prototype.slice.call(rowsBox.querySelectorAll("tr"));
  }

  function field(row, name) {
    return row.querySelector("input[name=" + name + "]");
  }

  function setRowError(row, text) {
    var error = row.querySelector("[data-row-error]");
    error.textContent = text;
    error.hidden = !text;
    row.classList.toggle("batch-row-bad", Boolean(text));
  }

  /* Столбец из таблицы приходит строками; две колонки (фамилия и имя) — через
   * табуляцию, она становится пробелом. */
  function pastedNames() {
    return namesInput.value.split(/\r?\n/).map(function (line) {
      return line.replace(/\s+/g, " ").trim();
    }).filter(Boolean);
  }

  form.querySelector("[data-batch-build]").addEventListener("click", function () {
    var names = pastedNames();
    showStatus("");
    if (!names.length) { showStatus(NO_NAMES_TEXT); return; }
    if (names.length > MAX_ROWS) { showStatus(TOO_MANY_TEXT); return; }
    var used = knownLogins();
    rowsBox.textContent = "";
    names.forEach(function (name) {
      var row = rowTemplate.content.firstElementChild.cloneNode(true);
      var login = freeLogin(Team.suggestLogin(name), used);
      if (login) used[login] = true;
      field(row, "invited_name").value = name;
      field(row, "login").value = login;
      rowsBox.appendChild(row);
    });
    countEl.textContent = String(names.length);
    tableBox.hidden = false;
  });

  rowsBox.addEventListener("input", function (event) {
    var row = event.target.closest("tr");
    if (row) setRowError(row, "");
  });

  /* Строки к отправке; null — у какой-то строки ошибка, она уже показана. */
  function checkedRows() {
    var seen = {};
    var bad = false;
    var rows = tableRows().map(function (row) {
      var name = field(row, "invited_name").value.trim();
      var raw = field(row, "login").value.trim();
      var login = raw.toUpperCase();
      var text = "";
      if (!name) text = NO_NAME_TEXT;
      else if (!Team.LOGIN_FORMAT.test(raw)) text = Team.LOGIN_FORMAT_TEXT;
      else if (seen[login]) text = DUPLICATE_TEXT;
      if (Team.LOGIN_FORMAT.test(raw)) seen[login] = true;
      setRowError(row, text);
      bad = bad || Boolean(text);
      return { invited_name: name, login: login };
    });
    return bad ? null : rows;
  }

  function showServerProblems(details) {
    var rows = tableRows();
    details.forEach(function (problem) {
      var row = rows[problem.row - 1];
      if (!row) return;
      var login = field(row, "login").value.trim().toUpperCase();
      setRowError(row, problem.reason === "taken" ? Team.loginTakenText(login)
        : problem.reason === "duplicate" ? DUPLICATE_TEXT : Team.LOGIN_FORMAT_TEXT);
    });
  }

  function showSheet(result) {
    var cards = sheet.querySelector("[data-sheet-cards]");
    var cardTemplate = sheet.querySelector("template[data-sheet-card]");
    cards.textContent = "";
    result.cards.forEach(function (card) {
      var node = cardTemplate.content.firstElementChild.cloneNode(true);
      node.querySelector("[data-card-name]").textContent = card.invited_name;
      node.querySelector("[data-card-login]").textContent = card.login;
      node.querySelector("[data-card-expires]").textContent = result.expires_local;
      node.querySelector("[data-card-qr]").innerHTML = card.qr_svg;
      cards.appendChild(node);
    });
    sheet.querySelector("[data-sheet-count]").textContent = String(result.cards.length);
    sheet.querySelector("[data-sheet-expires]").textContent = result.expires_local;
    form.reset();
    Team.syncServices(form, managerRole);
    rowsBox.textContent = "";
    tableBox.hidden = true;
    panel.hidden = true;
    openButton.setAttribute("aria-expanded", "false");
    sheet.hidden = false;
    sheet.scrollIntoView({ block: "start" });
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    showStatus("");
    var rows = checkedRows();
    if (rows === null) { showStatus(NOT_ISSUED_TEXT); return; }
    var serviceKeys = Team.chosenServices(form, managerRole);
    if (serviceKeys === null) return;
    Team.setBusy(true);
    try {
      showSheet(await Team.post(url, {
        role_key: Team.selectedRole(form), service_keys: serviceKeys, rows: rows
      }));
    } catch (error) {
      if (error.code === "ERR-AUTH-012" && error.details.length) {
        showServerProblems(error.details);
        showStatus(NOT_ISSUED_TEXT);
      } else {
        showStatus(error.message);
      }
    } finally {
      Team.setBusy(false);
    }
  });

  sheet.querySelector("[data-sheet-print]").addEventListener("click", function () {
    window.print();
  });
})();
