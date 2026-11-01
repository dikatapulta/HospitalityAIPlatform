// Демо «Номера» (issue #603): доска номеров этажей 13–25 и шаги уборки на
// выдуманных данных. Ни запросов к серверу, ни записи: состояние живёт в
// атрибутах клеток до перезагрузки страницы. Разметку и надписи не сочиняет —
// клонирует <template data-face> (лицо статуса) и <template data-steps>
// (кнопки шага, data-to — следующий статус из demo_room_board.STEPS) из
// demo_room_board.html; сам пишет только время шага по часам компьютера, а
// текст замечания — только через textContent. Счётчик сверху — фильтр по
// статусу (остальные клетки бледнеют), полоса этажей — фильтр по этажу; на
// телефоне всегда один этаж.
"use strict";

(() => {
  const root = document.querySelector("[data-rooms]");
  const panel = document.querySelector("[data-room-panel]");
  if (!root || !panel) return;

  const cells = Array.from(root.querySelectorAll("[data-room]"));
  const rows = Array.from(root.querySelectorAll(".rooms-row"));
  const statusButtons = Array.from(root.querySelectorAll("[data-filter-status]"));
  const floorButtons = Array.from(root.querySelectorAll("[data-filter-floor]"));
  const phone = window.matchMedia("(max-width: 699px)");
  const state = { status: "all", floor: "all", room: null };

  const template = (kind, status) =>
    document.querySelector(`template[data-${kind}="${status}"]`);

  const pad = (value) => String(value).padStart(2, "0");
  function clock() {
    const now = new Date();
    return `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  }

  // ---------- Клетка и счётчики ----------

  function paint(cell) {
    const face = template("face", cell.dataset.status);
    cell.querySelector("[data-room-face]").replaceChildren(face.content.cloneNode(true));
  }

  function recount() {
    const counts = { all: cells.length };
    for (const cell of cells) counts[cell.dataset.status] = (counts[cell.dataset.status] || 0) + 1;
    for (const output of root.querySelectorAll("[data-count]")) {
      output.textContent = String(counts[output.dataset.count] || 0);
    }
  }

  // ---------- Фильтры ----------

  function shownFloor() {
    // Телефон показывает один этаж: «Все» там нет — берётся первый.
    if (phone.matches && state.floor === "all") return floorButtons[1].dataset.filterFloor;
    return state.floor;
  }

  function applyFilters() {
    const floor = shownFloor();
    for (const row of rows) row.hidden = floor !== "all" && row.dataset.floor !== floor;
    let matches = 0;
    for (const cell of cells) {
      const onFloor = floor === "all" || cell.dataset.floor === floor;
      const match = state.status === "all" || cell.dataset.status === state.status;
      cell.classList.toggle("is-dim", !match);
      if (onFloor && match) matches += 1;
    }
    for (const button of statusButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.filterStatus === state.status));
    }
    for (const button of floorButtons) {
      button.setAttribute("aria-pressed", String(button.dataset.filterFloor === floor));
    }
    for (const empty of root.querySelectorAll("[data-rooms-empty]")) {
      const kind = floor === "all" ? "all" : "floor";
      empty.hidden = matches > 0 || empty.dataset.roomsEmpty !== kind;
    }
  }

  for (const button of statusButtons) {
    button.addEventListener("click", () => {
      const status = button.dataset.filterStatus;
      state.status = state.status === status ? "all" : status;
      applyFilters();
    });
  }
  for (const button of floorButtons) {
    button.addEventListener("click", () => {
      state.floor = button.dataset.filterFloor;
      applyFilters();
    });
  }
  phone.addEventListener("change", applyFilters);

  // ---------- Панель номера ----------

  const remarkForm = panel.querySelector("[data-remark-form]");
  const actions = panel.querySelector("[data-panel-actions]");

  function renderPanel() {
    const cell = state.room;
    const face = template("face", cell.dataset.status);
    panel.querySelector("[data-panel-number]").textContent = cell.dataset.room;
    panel.querySelector("[data-panel-floor]").textContent = cell.dataset.floor;
    panel.querySelector("[data-panel-icon]").replaceChildren(
      face.content.querySelector(".room-icon").firstElementChild.cloneNode(true),
    );
    panel.querySelector("[data-panel-label]").textContent = face.dataset.label;
    panel.querySelector("[data-panel-status]").dataset.tone = cell.dataset.status;
    panel.querySelector("[data-panel-line]").textContent = cell.dataset.line;
    const note = cell.dataset.note;
    panel.querySelector("[data-panel-note]").hidden = !note;
    panel.querySelector("[data-panel-note-text]").textContent = note;
    actions.replaceChildren(template("steps", cell.dataset.status).content.cloneNode(true));
    const lostFound = actions.querySelector("[data-lost-found]");
    if (lostFound) {
      lostFound.href = `${lostFound.getAttribute("href")}?room=${encodeURIComponent(cell.dataset.room)}`;
    }
    remarkForm.hidden = true;
    actions.hidden = false;
  }

  function moveRoom(button, note) {
    const cell = state.room;
    cell.dataset.status = button.dataset.to;
    cell.dataset.line = `${button.dataset.line} ${clock()}`;
    if (button.dataset.noteMode === "set") cell.dataset.note = note;
    if (button.dataset.noteMode === "clear") cell.dataset.note = "";
    paint(cell);
    cell.classList.remove("is-changed");
    void cell.offsetWidth; // перезапуск подсветки при втором шаге подряд
    cell.classList.add("is-changed");
    recount();
    applyFilters();
    renderPanel();
  }

  let remarkButton = null;
  actions.addEventListener("click", (event) => {
    const button = event.target.closest("[data-step]");
    if (!button) return;
    if (button.hasAttribute("data-remark")) {
      remarkButton = button;
      actions.hidden = true;
      remarkForm.hidden = false;
      remarkForm.elements.note.value = "";
      remarkForm.elements.note.focus();
      return;
    }
    moveRoom(button, "");
  });

  remarkForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = remarkForm.elements.note.value.trim();
    if (!text || !remarkButton) return;
    moveRoom(remarkButton, text);
  });
  panel.querySelector("[data-remark-cancel]").addEventListener("click", () => {
    remarkForm.hidden = true;
    actions.hidden = false;
  });

  function closePanel() {
    panel.close();
  }
  panel.querySelector("[data-panel-close]").addEventListener("click", closePanel);
  // Нажатие мимо панели (по затемнению) закрывает её, как Esc.
  panel.addEventListener("click", (event) => {
    if (event.target === panel) closePanel();
  });
  panel.addEventListener("close", () => {
    if (state.room) state.room.focus();
  });

  for (const cell of cells) {
    cell.addEventListener("click", () => {
      state.room = cell;
      renderPanel();
      panel.showModal();
    });
  }

  applyFilters();
})();
