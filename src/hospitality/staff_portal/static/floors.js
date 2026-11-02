/* Страница «Этажи» (spec 0053 §5, issue #478): доска раздачи (#660).
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Раздача живёт в разметке: карточка горничной на этаже и есть строка раздачи.
 * Компьютер — перетащить горничную из списка или карточку с этажа на этаж; телефон
 * и клавиатура — нажать на горничную (или карточку), потом на этаж. «✕» снимает с
 * этажа. Изменения копятся до «Сохранить раздачу»: тот же POST, что у полей ввода
 * до #660, строка «7, 8» на каждую горничную списка — разбирает сервер
 * (floors.parse_floors). После сохранения — перезагрузка с ?saved=1: строку
 * состояния рисует только Jinja.
 *
 * CSRF-контракт JSON-действий (докстринг router.py): каждый POST несёт
 * Content-Type: application/json — Origin браузер добавляет сам.
 */
(function () {
  "use strict";

  var board = document.getElementById("floors-board");
  var status = document.getElementById("floors-status");
  if (!board || !status) return;
  var endpoint = board.dataset.endpoint;
  var grid = board.querySelector(".floors-grid");
  var people = board.querySelectorAll(".floors-person");
  var emptyLine = document.getElementById("floors-board-empty");
  var dirtyBar = document.getElementById("floors-dirty");
  var saveButton = dirtyBar.querySelector("[data-save]");
  var cardTemplate = document.getElementById("floor-card-template");
  var cellTemplate = document.getElementById("floor-cell-template");
  var addForm = document.getElementById("floors-add");
  var addInput = document.getElementById("floors-add-input");
  var addError = document.getElementById("floors-add-error");

  var MESSAGES = {
    "ERR-HOUSEKEEPING-003": "Раздаёт этажи старший хаускипинга или менеджер.",
    "ERR-HOUSEKEEPING-006": "Состав хаускипинга изменился — обновите страницу и раздайте ещё раз.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново."
  };

  var dirty = false;
  /* Выбранное нажатием: { userId, name, from } — from = этаж карточки или null (список). */
  var picked = null;
  /* Что сейчас тащат мышью — то же, что picked. */
  var dragged = null;

  function showStatus(text, ok) {
    status.textContent = text;
    status.className = ok ? "hint ok" : "error";
    status.hidden = !text;
  }

  function cellOf(floor) {
    return grid.querySelector('.floor-cell[data-floor="' + floor + '"]');
  }

  function cardIn(cell, userId) {
    return cell.querySelector('.floor-card[data-user-id="' + userId + '"]');
  }

  /* Этажи горничной по карточкам доски, по возрастанию. */
  function floorsOf(userId) {
    var cards = grid.querySelectorAll('.floor-card[data-user-id="' + userId + '"]');
    return Array.prototype.map.call(cards, function (card) {
      return Number(card.closest(".floor-cell").dataset.floor);
    }).sort(function (a, b) { return a - b; });
  }

  /* «7, 8, 12-14»: подряд от трёх — диапазоном, как floors.format_floors. */
  function formatFloors(floors) {
    var runs = [];
    floors.forEach(function (floor) {
      var run = runs[runs.length - 1];
      if (run && floor === run[run.length - 1] + 1) run.push(floor);
      else runs.push([floor]);
    });
    return runs.map(function (run) {
      return run.length >= 3 ? run[0] + "-" + run[run.length - 1] : run.join(", ");
    }).join(", ");
  }

  /* Подпись в списке — та же, что floors_view.floors_line. */
  function floorsLine(floors) {
    if (!floors.length) return "без этажа";
    if (floors.length === 1) return "этаж " + floors[0];
    return "этажи " + formatFloors(floors);
  }

  function refreshPerson(userId) {
    var person = board.querySelector('.floors-person[data-user-id="' + userId + '"]');
    if (person) person.querySelector("[data-person-floors]").textContent = floorsLine(floorsOf(userId));
  }

  function refreshCell(cell) {
    cell.querySelector(".floor-empty").hidden = !!cell.querySelector(".floor-card");
  }

  function markDirty() {
    if (dirty) return;
    dirty = true;
    dirtyBar.hidden = false;
    showStatus("", false);
  }

  /* Карточка по шаблону — одна разметка с Jinja (макрос floor_card). */
  function newCard(userId, name, floor) {
    var card = cardTemplate.content.firstElementChild.cloneNode(true);
    card.dataset.userId = userId;
    card.dataset.name = name;
    card.querySelector(".floor-card-pick").textContent = name;
    card.querySelector(".floor-card-remove").setAttribute("aria-label", "Снять с " + floor + " этажа");
    return card;
  }

  /* Карточки этажа — по имени, как рисует сервер. */
  function insertCard(cell, card) {
    var cards = cell.querySelector(".floor-cards");
    var name = card.dataset.name.toLowerCase();
    var before = Array.prototype.find.call(cards.children, function (other) {
      return other.dataset.name.toLowerCase() > name;
    });
    cards.insertBefore(card, before || null);
  }

  /* Поставить горничную на этаж: из списка — добавить, с этажа — перенести. */
  function place(source, floor) {
    var target = cellOf(floor);
    if (!target || String(source.from) === String(floor)) return;
    if (source.from === null && cardIn(target, source.userId)) return;  // уже на этом этаже
    if (source.from !== null) {
      var origin = cellOf(source.from);
      var old = origin && cardIn(origin, source.userId);
      if (old) {
        old.remove();
        refreshCell(origin);
      }
    }
    if (!cardIn(target, source.userId)) insertCard(target, newCard(source.userId, source.name, floor));
    refreshCell(target);
    refreshPerson(source.userId);
    markDirty();
  }

  function unpick() {
    if (!picked) return;
    board.querySelectorAll('[aria-pressed="true"]').forEach(function (button) {
      button.setAttribute("aria-pressed", "false");
    });
    board.classList.remove("is-picking");
    picked = null;
  }

  function pick(button, source) {
    var again = picked && picked.button === button;
    unpick();
    if (again) return;
    picked = source;
    picked.button = button;
    button.setAttribute("aria-pressed", "true");
    board.classList.add("is-picking");
  }

  function sourceOf(element) {
    var card = element.closest(".floor-card");
    if (card) {
      return { userId: card.dataset.userId, name: card.dataset.name, from: card.closest(".floor-cell").dataset.floor };
    }
    var person = element.closest(".floors-person");
    return person ? { userId: person.dataset.userId, name: person.dataset.name, from: null } : null;
  }

  board.addEventListener("click", function (event) {
    var remove = event.target.closest(".floor-card-remove");
    if (remove) {
      var card = remove.closest(".floor-card");
      var cell = card.closest(".floor-cell");
      unpick();
      card.remove();
      refreshCell(cell);
      refreshPerson(card.dataset.userId);
      markDirty();
      return;
    }
    var chooser = event.target.closest(".floors-person, .floor-card-pick");
    var cellTarget = event.target.closest(".floor-cell");
    /* Выбрана горничная, нажали на этаж (в том числе на чужую карточку на нём) — ставим. */
    if (picked && cellTarget && !(chooser && chooser === picked.button)) {
      var source = picked;
      unpick();
      place(source, cellTarget.dataset.floor);
      return;
    }
    if (chooser) pick(chooser, sourceOf(chooser));
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") unpick();
  });

  board.addEventListener("dragstart", function (event) {
    var source = sourceOf(event.target);
    if (!source) return;
    unpick();
    dragged = source;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", source.name);  // без данных Firefox не тащит
  });

  board.addEventListener("dragend", function () {
    dragged = null;
    grid.querySelectorAll(".is-over").forEach(function (cell) { cell.classList.remove("is-over"); });
  });

  grid.addEventListener("dragover", function (event) {
    var cell = event.target.closest(".floor-cell");
    if (!dragged || !cell) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    cell.classList.add("is-over");
  });

  grid.addEventListener("dragleave", function (event) {
    var cell = event.target.closest(".floor-cell");
    if (cell && !cell.contains(event.relatedTarget)) cell.classList.remove("is-over");
  });

  grid.addEventListener("drop", function (event) {
    var cell = event.target.closest(".floor-cell");
    if (!dragged || !cell) return;
    event.preventDefault();
    cell.classList.remove("is-over");
    place(dragged, cell.dataset.floor);
    dragged = null;
  });

  /* «Добавить этаж»: только на время страницы — сохраняется раздача, а не этаж (§5). */
  addForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = addInput.value.trim();
    var floor = /^[0-9]{1,2}$/.test(text) ? Number(text) : 0;
    if (floor < 1) {
      addInput.setAttribute("aria-invalid", "true");
      addError.textContent = "Этаж — число от 1 до 99.";
      addError.hidden = false;
      addInput.focus();
      return;
    }
    addInput.removeAttribute("aria-invalid");
    addError.hidden = true;
    addInput.value = "";
    var cell = cellOf(floor);
    if (!cell) {
      cell = cellTemplate.content.firstElementChild.cloneNode(true);
      cell.dataset.floor = String(floor);
      cell.querySelector(".floor-cell-title").textContent = floor + " этаж";
      var after = Array.prototype.find.call(grid.children, function (other) {
        return Number(other.dataset.floor) > floor;
      });
      grid.insertBefore(cell, after || null);
      emptyLine.hidden = true;
    }
    cell.scrollIntoView({ block: "nearest" });
    cell.querySelector(".floor-cell-title").focus();
  });

  addInput.addEventListener("input", function () {
    addInput.removeAttribute("aria-invalid");
    addError.hidden = true;
  });

  /* POST по CSRF-контракту; вернуть { ok, status, code }. */
  async function send(url, body) {
    try {
      var response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не JSON */ }
      var code = !response.ok && data && data.error ? data.error.code : "";
      return { ok: response.ok, status: response.status, code: code };
    } catch (error) {
      return { ok: false, status: 0, code: "network" };
    }
  }

  saveButton.addEventListener("click", async function () {
    unpick();
    var rows = Array.prototype.map.call(people, function (person) {
      return { user_id: person.dataset.userId, floors: floorsOf(person.dataset.userId).join(", ") };
    });
    showStatus("", false);
    saveButton.disabled = true;
    var result = await send(endpoint, { rows: rows });
    if (result.ok) {
      dirty = false;
      window.location.href = window.location.pathname + "?saved=1";
      return;
    }
    saveButton.disabled = false;
    if (result.code === "network") {
      showStatus("Нет связи — попробуйте ещё раз.", false);
    } else {
      showStatus(MESSAGES[result.code] ||
        "Не получилось (" + (result.code || result.status) + "). Попробуйте ещё раз.", false);
    }
    status.scrollIntoView({ block: "nearest" });
  });

  /* «Отменить» — вернуть сохранённую раздачу: страница заново, без ?saved. */
  dirtyBar.querySelector("[data-cancel]").addEventListener("click", function () {
    dirty = false;
    window.location.href = window.location.pathname;
  });

  window.addEventListener("beforeunload", function (event) {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
})();
