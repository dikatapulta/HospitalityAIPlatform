// Демо «Передача смены», вкладка «Лист передачи» (issue #452), и переключатель
// вкладок «Касса» / «Лист передачи». Ни запросов к серверу, ни записи: правки
// и примечания живут в памяти страницы до перезагрузки. Разметку не сочиняет —
// клонирует <template> из _handover_board.html и пишет только textContent,
// поэтому введённый текст в HTML не попадает. Каждая правка строки, смена
// статуса и примечание — запись в истории строки и в ленте «Изменения за
// смену» с логином вошедшего (data-me) и временем по часам компьютера.
"use strict";

(() => {
  // ---------- Вкладки ----------
  const tabs = Array.from(document.querySelectorAll("[data-handover-tab]"));

  function selectTab(name, focus) {
    for (const tab of tabs) {
      const on = tab.dataset.handoverTab === name;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !on;
      if (on && focus) tab.focus();
    }
  }

  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener("click", () => {
      selectTab(tab.dataset.handoverTab, false);
      const hash = tab.dataset.handoverTab === "list" ? "#list" : "";
      history.replaceState(null, "", location.pathname + location.search + hash);
    });
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      tabs[(index + step + tabs.length) % tabs.length].click();
      tabs[(index + step + tabs.length) % tabs.length].focus();
    });
  }
  if (location.hash === "#list") selectTab("list", false);

  // ---------- Лист ----------
  const board = document.querySelector("[data-board]");
  if (!board) return;
  const me = board.dataset.me;
  const list = board.querySelector("[data-board-list]");
  const feed = board.querySelector("[data-board-feed]");
  const feedPanel = board.querySelector("[data-board-feed-panel]");
  const feedMore = board.querySelector("[data-board-feed-more]");
  const empty = board.querySelector("[data-board-empty]");
  const filters = Array.from(board.querySelectorAll("[data-board-filter]"));
  const templates = {};
  for (const template of board.querySelectorAll("template[data-board-template]")) {
    templates[template.dataset.boardTemplate] = template;
  }
  const rowTemplate = templates.row.content;
  const kinds = new Map(
    Array.from(rowTemplate.querySelectorAll("select[name=kind] option"), (option) => [
      option.value,
      { icon: option.dataset.icon, label: option.dataset.label },
    ]),
  );
  const signs = new Map(
    Array.from(rowTemplate.querySelectorAll("input[name=currency]"), (radio) => [radio.value, radio.dataset.sign]),
  );
  const motion = !matchMedia("(prefers-reduced-motion: reduce)").matches;
  let filter = "all";
  let created = 0;

  // Время записей — часы компьютера на стойке, как дата и смена бланка кассы.
  const now = new Date();
  const clock = (date) => date.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  for (const el of board.querySelectorAll("time[data-ago]")) {
    if (el.dataset.ago !== "") el.textContent = clock(new Date(now.getTime() - Number(el.dataset.ago) * 60000));
  }
  board.querySelector("[data-board-date]").textContent = now.toLocaleDateString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
  board.querySelector("[data-board-shift]").textContent =
    now.getHours() >= 8 && now.getHours() < 20 ? "08:00–20:00" : "20:00–08:00";

  const group = new Intl.NumberFormat("ru-RU");
  const money = (amount, currency) =>
    amount === "" ? "без суммы" : `${group.format(Number(amount))} ${signs.get(currency)}`;
  const field = (row, name) => row.querySelector(`[data-field="${name}"]`);
  const rows = () => Array.from(list.querySelectorAll("[data-board-row]"));
  const state = (row) => ({
    room: row.dataset.room,
    kind: row.dataset.kind,
    summary: row.dataset.summary,
    amount: row.dataset.amount,
    currency: row.dataset.currency,
  });

  function flash(el, name = "is-flash") {
    el.classList.remove(name);
    void el.offsetWidth; // перезапуск анимации на том же элементе
    el.classList.add(name);
    return el;
  }

  function clone(name, values) {
    const node = templates[name].content.firstElementChild.cloneNode(true);
    for (const [key, value] of Object.entries(values)) {
      const el = node.querySelector(`[data-entry-${key}]`);
      if (el) el.textContent = value;
    }
    return node;
  }

  function render(row) {
    const s = state(row);
    const kind = kinds.get(s.kind);
    const closed = row.dataset.status === "closed";
    field(row, "icon").textContent = kind.icon;
    field(row, "room").textContent = s.room;
    field(row, "kind").textContent = kind.label;
    field(row, "summary").textContent = s.summary;
    field(row, "summary").hidden = !s.summary;
    field(row, "money").textContent = money(s.amount, s.currency);
    field(row, "status").textContent = closed ? "Закрыт" : "Открыт";
    row.querySelector('[data-board-action="toggle"]').textContent = closed ? "Открыть снова" : "Закрыть";
    row.classList.toggle("is-closed", closed);
    for (const key of kinds.keys()) row.classList.toggle(`board-kind-${key}`, key === s.kind);
  }

  // «сумма 15 000 → 20 000 ₸»; при смене валюты — обе суммы целиком.
  function amountChange(before, after) {
    const side = (s) => (s.amount === "" ? "—" : money(s.amount, s.currency));
    if (before.currency === after.currency && before.amount !== "" && after.amount !== "") {
      return `сумма ${group.format(Number(before.amount))} → ${money(after.amount, after.currency)}`;
    }
    return `сумма ${side(before)} → ${side(after)}`;
  }

  function changes(before, after) {
    const texts = [];
    if (before.room !== after.room) texts.push(`номер: ${before.room} → ${after.room}`);
    if (before.kind !== after.kind) {
      texts.push(`тип: ${kinds.get(before.kind).label} → ${kinds.get(after.kind).label}`);
    }
    if (before.summary !== after.summary) {
      texts.push(`суть: «${before.summary || "—"}» → «${after.summary || "—"}»`);
    }
    if (before.amount !== after.amount || before.currency !== after.currency) {
      texts.push(amountChange(before, after));
    }
    return texts;
  }

  function feedEntry(row, values, note) {
    const node = clone("feed", { ...values, room: row.dataset.room });
    const button = node.querySelector("[data-feed-row]");
    button.dataset.feedRow = row.dataset.boardRow;
    button.classList.toggle("is-note", note);
    feed.prepend(flash(node, "is-new"));
  }

  function log(row, texts) {
    const time = clock(new Date());
    const history = row.querySelector("[data-board-history]");
    for (const text of texts) {
      history.append(flash(clone("event", { login: me, time, text }), "is-new"));
      feedEntry(row, { login: me, time, text }, false);
    }
    row.querySelector("[data-last-login]").textContent = me;
    row.querySelector("[data-last-time]").textContent = time;
    row.querySelector("[data-last-text]").textContent = texts[texts.length - 1];
    // Одно «внёс строку» уже сказано подписью «Внёс …» внизу строки.
    const last = row.querySelector(".board-last");
    last.hidden = history.children.length < 2;
    flash(last, "is-changed");
    recount();
  }

  function recount() {
    const saved = rows().filter((row) => row.dataset.boardNew !== "true");
    const open = saved.filter((row) => row.dataset.status !== "closed");
    const counts = { all: saved.length, open: open.length, closed: saved.length - open.length };
    for (const el of document.querySelectorAll("[data-board-count]")) el.textContent = counts[el.dataset.boardCount];
    const totals = new Map(Array.from(signs.keys(), (currency) => [currency, 0]));
    for (const row of open) {
      if (row.dataset.amount !== "") {
        totals.set(row.dataset.currency, totals.get(row.dataset.currency) + Number(row.dataset.amount));
      }
    }
    const parts = Array.from(totals)
      .filter(([currency, total]) => total > 0 || currency === "KZT")
      .map(([currency, total]) => money(String(total), currency));
    board.querySelector("[data-board-total]").textContent = parts.join(" · ");
    for (const row of rows()) {
      row.querySelector('[data-count="history"]').textContent = row.querySelector("[data-board-history]").children.length;
      row.querySelector('[data-count="notes"]').textContent = row.querySelector("[data-board-notes]").children.length;
    }
    const entries = feed.children.length;
    for (const el of board.querySelectorAll("[data-board-feed-count]")) el.textContent = entries;
    if (!feedPanel.classList.contains("is-expanded")) feedMore.textContent = `Все изменения · ${entries}`;
  }

  function applyFilter() {
    let visible = 0;
    for (const row of rows()) {
      const show = filter === "all" || row.dataset.boardNew === "true" || row.dataset.status === filter;
      row.hidden = !show;
      if (show) visible += 1;
    }
    empty.hidden = visible > 0;
    for (const option of filters) {
      option.setAttribute("aria-pressed", String(option.dataset.boardFilter === filter));
    }
  }

  function openEdit(row) {
    const form = row.querySelector("[data-board-edit]");
    const s = state(row);
    form.elements.room.value = s.room;
    form.elements.kind.value = s.kind;
    form.elements.summary.value = s.summary;
    form.elements.amount.value = s.amount;
    form.elements.currency.value = s.currency;
    showError(form, "");
    form.hidden = false;
    row.classList.add("is-editing");
    form.elements.room.focus();
  }

  function closeEdit(row) {
    row.querySelector("[data-board-edit]").hidden = true;
    row.classList.remove("is-editing");
    row.querySelector('[data-board-action="edit"]').focus();
  }

  function showError(form, text, input) {
    const error = form.querySelector("[data-board-error]");
    error.textContent = text;
    error.hidden = !text;
    if (input) input.focus();
  }

  function save(row, form) {
    const room = form.elements.room.value.trim();
    const amount = form.elements.amount.value.replace(/\s/g, "");
    if (!room) return showError(form, "Укажите номер", form.elements.room);
    if (amount && !/^\d{1,9}$/.test(amount)) {
      return showError(form, "Сумма — только цифры", form.elements.amount);
    }
    const before = state(row);
    const after = {
      room,
      kind: form.elements.kind.value,
      summary: form.elements.summary.value.trim(),
      amount: amount === "" ? "" : String(Number(amount)),
      currency: form.elements.currency.value,
    };
    const fresh = row.dataset.boardNew === "true";
    const texts = fresh ? ["внёс строку"] : changes(before, after);
    Object.assign(row.dataset, after);
    if (fresh) {
      delete row.dataset.boardNew;
      field(row, "author").textContent = me;
      field(row, "created").textContent = clock(new Date());
    }
    render(row);
    closeEdit(row);
    if (texts.length) log(row, texts);
    for (const [name, value] of Object.entries(after)) {
      if (!fresh && before[name] !== value) {
        const target = field(row, { kind: "kind", amount: "money", currency: "money" }[name] || name);
        if (target) flash(target, "is-changed");
      }
    }
    applyFilter();
  }

  function cancel(row) {
    if (row.dataset.boardNew === "true") {
      row.remove();
      applyFilter();
      board.querySelector("[data-board-add]").focus();
      return;
    }
    closeEdit(row);
  }

  function toggle(row) {
    const closing = row.dataset.status !== "closed";
    row.dataset.status = closing ? "closed" : "open";
    render(row);
    log(row, [closing ? "статус: открыт → закрыт" : "статус: закрыт → открыт"]);
    flash(field(row, "status"), "is-changed");
    applyFilter();
  }

  function addNote(row, form) {
    const text = form.elements.note.value.trim();
    if (!text) return;
    const time = clock(new Date());
    row.querySelector("[data-board-notes]").append(flash(clone("note", { login: me, time, text }), "is-new"));
    feedEntry(row, { login: me, time, text }, true);
    form.elements.note.value = "";
    recount();
  }

  list.addEventListener("click", (event) => {
    const button = event.target.closest("[data-board-action]");
    if (!button) return;
    const row = button.closest("[data-board-row]");
    const action = button.dataset.boardAction;
    if (action === "edit") openEdit(row);
    else if (action === "toggle") toggle(row);
    else if (action === "cancel") cancel(row);
  });
  list.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    const row = form.closest("[data-board-row]");
    if (form.matches("[data-board-note-form]")) addNote(row, form);
    else if (form.matches("[data-board-edit]")) save(row, form);
  });
  list.addEventListener("keydown", (event) => {
    const form = event.key === "Escape" && event.target.closest("[data-board-edit]");
    if (form) cancel(form.closest("[data-board-row]"));
  });

  board.querySelector("[data-board-add]").addEventListener("click", () => {
    filter = "all";
    created += 1;
    const row = rowTemplate.firstElementChild.cloneNode(true);
    row.dataset.boardRow = `new${created}`;
    row.dataset.boardNew = "true";
    list.prepend(row);
    applyFilter();
    openEdit(row);
  });

  for (const option of filters) {
    option.addEventListener("click", () => {
      filter = option.dataset.boardFilter;
      applyFilter();
    });
  }

  feedMore.addEventListener("click", () => {
    const expanded = feedPanel.classList.toggle("is-expanded");
    feedMore.textContent = expanded ? "Свернуть" : `Все изменения · ${feed.children.length}`;
  });

  feed.addEventListener("click", (event) => {
    const item = event.target.closest("[data-feed-row]");
    const row = item && list.querySelector(`[data-board-row="${CSS.escape(item.dataset.feedRow)}"]`);
    if (!row) return;
    if (row.hidden) {
      filter = "all";
      applyFilter();
    }
    row.scrollIntoView({ behavior: motion ? "smooth" : "auto", block: "center" });
    flash(row);
  });
})();
