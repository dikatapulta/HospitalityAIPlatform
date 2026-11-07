/* Доска «Номера» (spec 0061 §4, issue #666): все номера отеля по этажам.
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Разметка доски живёт только в Jinja (_room_board.html): обновление — замена
 * innerHTML #room-board ответом fragment-эндпоинта каждые 15 с, пауза на скрытой
 * вкладке и ПОКА ОТКРЫТА ПАНЕЛЬ НОМЕРА — обновление не закрывает её под пальцем.
 * Фильтры статуса (счётчики) и этажа живут здесь и переживают обновление; на
 * телефоне всегда один этаж. Кнопки панели — клоны <template data-steps> (права
 * решил сервер), действия — JSON turnover_api через turnover_actions.js; после
 * шага доска перечитывается сразу, а панель показывает новый статус номера.
 * Строки панели и замечание — только textContent.
 */
"use strict";

(() => {
  const POLL_INTERVAL_MS = 15000;

  const root = document.getElementById("room-board");
  const panel = document.querySelector("[data-room-panel]");
  const api = window.TurnoverActions;
  if (!root || !panel || !api) return;
  const endpoint = root.dataset.endpoint;
  const fragmentUrl = root.dataset.fragmentUrl;
  const phone = window.matchMedia("(max-width: 699px)");
  const state = { status: "all", floor: "all", room: null };

  const all = (selector) => Array.from(root.querySelectorAll(selector));
  const findCell = (number) => all("[data-room]").find((cell) => cell.dataset.room === number);

  // ---------- Фильтры ----------

  function shownFloor() {
    // Телефон показывает один этаж: «Все» там нет — берётся первый ряд.
    if (phone.matches && state.floor === "all") {
      const first = all("[data-filter-floor]")[1];
      return first ? first.dataset.filterFloor : "all";
    }
    return state.floor;
  }

  function applyFilters() {
    // Выбранного ряда или счётчика после обновления может не стать — тогда «Все».
    if (state.floor !== "all" && !all(".rooms-row").some((row) => row.dataset.floor === state.floor)) {
      state.floor = "all";
    }
    if (state.status !== "all" &&
        !all("[data-filter-status]").some((button) => button.dataset.filterStatus === state.status)) {
      state.status = "all";
    }
    const floor = shownFloor();
    for (const row of all(".rooms-row")) row.hidden = floor !== "all" && row.dataset.floor !== floor;
    let matches = 0;
    for (const cell of all("[data-room]")) {
      const onFloor = floor === "all" || cell.dataset.floor === floor;
      const match = state.status === "all" || cell.dataset.status === state.status;
      cell.classList.toggle("is-dim", !match);
      if (onFloor && match) matches += 1;
    }
    for (const button of all("[data-filter-status]")) {
      button.setAttribute("aria-pressed", String(button.dataset.filterStatus === state.status));
    }
    for (const button of all("[data-filter-floor]")) {
      button.setAttribute("aria-pressed", String(button.dataset.filterFloor === floor));
    }
    for (const empty of all("[data-rooms-empty]")) {
      const kind = floor === "all" ? "all" : "floor";
      empty.hidden = matches > 0 || empty.dataset.roomsEmpty !== kind;
    }
  }

  // Счётчики, этажи и клетки перерисовывает каждое обновление — слушатель один, на контейнере.
  root.addEventListener("click", (event) => {
    const statusButton = event.target.closest("[data-filter-status]");
    if (statusButton) {
      const status = statusButton.dataset.filterStatus;
      state.status = state.status === status ? "all" : status;
      applyFilters();
      return;
    }
    const floorButton = event.target.closest("[data-filter-floor]");
    if (floorButton) {
      state.floor = floorButton.dataset.filterFloor;
      applyFilters();
      return;
    }
    const cell = event.target.closest("[data-room]");
    if (cell) openPanel(cell);
  });
  phone.addEventListener("change", applyFilters);

  // ---------- Обновление ----------

  let refreshing = false;
  async function refresh(force) {
    if (refreshing || (!force && panel.open)) return;
    refreshing = true;
    try {
      const response = await fetch(fragmentUrl, { cache: "no-store" });
      if (response.redirected) {
        // Сессия истекла: фрагмент ответил редиректом на вход — уходим туда же.
        window.location.href = response.url;
        return;
      }
      if (response.ok) {
        root.innerHTML = await response.text();
        applyFilters();
      }
    } catch (error) {
      // Нет сети — тик пропущен, следующий поллинг попробует снова.
    } finally {
      refreshing = false;
    }
  }

  setInterval(() => {
    if (!document.hidden) refresh(false);
  }, POLL_INTERVAL_MS);

  // ---------- Панель номера ----------

  const actions = panel.querySelector("[data-panel-actions]");
  const panelError = panel.querySelector("[data-panel-error]");
  const remarkForm = panel.querySelector("[data-remark-form]");
  const remarkError = remarkForm.querySelector("[data-remark-error]");
  const field = (name) => panel.querySelector(`[data-panel-${name}]`);

  function showPanelError(text) {
    panelError.textContent = text;
    panelError.hidden = !text;
  }

  // Пустое замечание — ошибка под полем, а не подсказка браузера (#640).
  function showRemarkError(on) {
    remarkError.hidden = !on;
    if (on) remarkForm.elements.note.setAttribute("aria-invalid", "true");
    else remarkForm.elements.note.removeAttribute("aria-invalid");
  }

  function renderPanel() {
    const cell = state.room;
    const data = cell.dataset;
    field("number").textContent = data.room;
    field("place").textContent = data.place;
    field("icon").replaceChildren(cell.querySelector(".room-icon").firstElementChild.cloneNode(true));
    field("label").textContent = data.label;
    field("status").dataset.tone = data.status;
    field("line").textContent = data.line;
    field("type").hidden = !data.type;
    field("type-code").textContent = data.type;
    field("guests").hidden = !data.guests;
    field("guests-count").textContent = data.guests;
    field("note").hidden = !data.note;
    field("note-text").textContent = data.note;
    const steps = document.querySelector(`template[data-steps="${data.status}"]`);
    actions.replaceChildren(steps ? steps.content.cloneNode(true) : "");
    remarkForm.hidden = true;
    actions.hidden = false;
  }

  function openPanel(cell) {
    state.room = cell;
    showPanelError("");
    renderPanel();
    panel.showModal();
  }

  let busy = false;
  async function run(action, body) {
    const number = state.room.dataset.room;
    const turnover = state.room.dataset.turnover;
    busy = true;
    for (const button of panel.querySelectorAll("button[data-action], [data-remark-form] button")) {
      button.disabled = true;
    }
    try {
      const result = action === "report"
        ? await api.report(endpoint, number, false)
        : await api.send(`${endpoint}/${turnover}/${action}`, body);
      if (result === null) {
        // Выселять гостя передумали — панель как была.
        renderPanel();
        return;
      }
      showPanelError(result.ok ? "" : api.failureText(result, action));
      // Сигнал кабинета (signal.js) перечитывает счётчик номеров сразу, а не через 15 с.
      document.dispatchEvent(new Event("staff-turnovers-changed"));
      await refresh(true);
      const fresh = findCell(number);
      if (fresh) {
        state.room = fresh;
        fresh.classList.add("is-changed");
      }
      if (panel.open) renderPanel();
    } finally {
      busy = false;
    }
  }

  actions.addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button || busy) return;
    const action = button.dataset.action;
    if (action === "remark") {
      actions.hidden = true;
      remarkForm.hidden = false;
      remarkForm.elements.note.value = "";
      showRemarkError(false);
      remarkForm.elements.note.focus();
      return;
    }
    if (action === "cancel" && !window.confirm(`Отменить выезд из ${state.room.dataset.room}?`)) {
      return;
    }
    run(action, {});
  });

  remarkForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    const text = remarkForm.elements.note.value.trim();
    if (!text) {
      showRemarkError(true);
      remarkForm.elements.note.focus();
      return;
    }
    run("inspect", { note: text });
  });
  remarkForm.elements.note.addEventListener("input", () => showRemarkError(false));
  panel.querySelector("[data-remark-cancel]").addEventListener("click", () => {
    remarkForm.hidden = true;
    actions.hidden = false;
  });

  panel.querySelector("[data-panel-close]").addEventListener("click", () => panel.close());
  // Нажатие мимо панели (по затемнению) закрывает её, как Esc.
  panel.addEventListener("click", (event) => {
    if (event.target === panel) panel.close();
  });
  panel.addEventListener("close", () => {
    // Клетку могло заменить обновление — фокус на её свежую копию.
    const cell = state.room && findCell(state.room.dataset.room);
    if (cell) cell.focus();
  });

  applyFilters();
})();
