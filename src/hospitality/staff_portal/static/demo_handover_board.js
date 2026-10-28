// Демо «Передача смены», вкладка «Лист передачи» (issue #452, компактный вид —
// #457), и переключатель вкладок «Касса» / «Лист передачи». Ни запросов к
// серверу, ни записи: правки и примечания живут в памяти страницы до
// перезагрузки. Разметку не сочиняет — клонирует <template> из
// _handover_board.html и пишет только textContent, поэтому введённый текст в
// HTML не попадает. Лист — одна строка на хвост; нажатие раскрывает строку на
// месте (правка, история, примечания), раскрыта всегда одна. Закрытая строка
// уезжает в свёрнутый блок «Закрыто». Каждая правка, смена статуса и
// примечание — запись в истории строки и в ленте «Изменения за смену» с
// логином вошедшего (data-me) и временем по часам компьютера.
"use strict";

(() => {
  // ---------- Вкладки ----------
  const tabs = Array.from(document.querySelectorAll("[data-handover-tab]"));

  function selectTab(name) {
    for (const tab of tabs) {
      const on = tab.dataset.handoverTab === name;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      document.getElementById(tab.getAttribute("aria-controls")).hidden = !on;
    }
  }

  for (const [index, tab] of tabs.entries()) {
    tab.addEventListener("click", () => {
      selectTab(tab.dataset.handoverTab);
      const hash = tab.dataset.handoverTab === "list" ? "#list" : "";
      history.replaceState(null, "", location.pathname + location.search + hash);
    });
    tab.addEventListener("keydown", (event) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
      if (!step) return;
      event.preventDefault();
      const next = tabs[(index + step + tabs.length) % tabs.length];
      next.click();
      next.focus();
    });
  }
  if (location.hash === "#list") selectTab("list");

  // ---------- Лист ----------
  const board = document.querySelector("[data-board]");
  if (!board) return;
  const me = board.dataset.me;
  const openList = board.querySelector("[data-board-list]");
  const closedList = board.querySelector("[data-board-closed]");
  const feed = board.querySelector("[data-board-feed]");
  const empty = board.querySelector("[data-board-empty]");
  const addButton = board.querySelector("[data-board-add]");
  const templates = {};
  for (const template of board.querySelectorAll("template[data-board-template]")) {
    templates[template.dataset.boardTemplate] = template;
  }
  const rowTemplate = templates.row.content;
  const kinds = new Map(
    Array.from(rowTemplate.querySelectorAll("select[name=kind] option"), (option) => [option.value, option.textContent]),
  );
  const signs = new Map(
    Array.from(rowTemplate.querySelectorAll("input[name=currency]"), (radio) => [radio.value, radio.dataset.sign]),
  );
  const motion = !matchMedia("(prefers-reduced-motion: reduce)").matches;
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
  const money = (amount, currency) => (amount === "" ? "—" : `${group.format(Number(amount))} ${signs.get(currency)}`);
  const field = (row, name) => row.querySelector(`[data-field="${name}"]`);
  const rows = () => Array.from(board.querySelectorAll(".board-list > [data-board-row]"));
  const isNew = (row) => row.dataset.boardNew === "true";
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
    const closed = row.dataset.status === "closed";
    field(row, "room").textContent = s.room;
    field(row, "kind").textContent = kinds.get(s.kind);
    field(row, "summary").textContent = s.summary;
    field(row, "money").textContent = money(s.amount, s.currency);
    const check = row.querySelector('[data-board-action="toggle"]');
    check.textContent = closed ? "↺" : "✓";
    check.title = closed ? "Открыть снова" : "Закрыть";
    check.setAttribute("aria-label", check.title);
    row.classList.toggle("is-closed", closed);
    for (const key of kinds.keys()) row.classList.toggle(`board-kind-${key}`, key === s.kind);
  }

  // «сумма 15 000 → 20 000 ₸»; при смене валюты — обе суммы целиком.
  function amountChange(before, after) {
    if (before.currency === after.currency && before.amount !== "" && after.amount !== "") {
      return `сумма ${group.format(Number(before.amount))} → ${money(after.amount, after.currency)}`;
    }
    return `сумма ${money(before.amount, before.currency)} → ${money(after.amount, after.currency)}`;
  }

  function changes(before, after) {
    const texts = [];
    if (before.room !== after.room) texts.push(`номер: ${before.room} → ${after.room}`);
    if (before.kind !== after.kind) texts.push(`тип: ${kinds.get(before.kind)} → ${kinds.get(after.kind)}`);
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

  function stamp(row, time) {
    row.querySelector("[data-last-login]").textContent = me;
    row.querySelector("[data-last-time]").textContent = time;
  }

  function log(row, texts) {
    const time = clock(new Date());
    const history = row.querySelector("[data-board-history]");
    for (const text of texts) {
      history.append(flash(clone("event", { login: me, time, text }), "is-new"));
      feedEntry(row, { login: me, time, text }, false);
    }
    stamp(row, time);
    recount();
  }

  function recount() {
    const saved = rows().filter((row) => !isNew(row));
    const open = saved.filter((row) => row.dataset.status !== "closed");
    const counts = { open: open.length, closed: saved.length - open.length };
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
      const notes = row.querySelector("[data-board-notes]").children.length;
      row.querySelector('[data-count="notes"]').textContent = notes;
      row.querySelector(".board-note-count").hidden = notes === 0;
    }
    board.querySelector("[data-board-feed-count]").textContent = feed.children.length;
    empty.hidden = openList.children.length > 0;
  }

  // ---------- Раскрытие строки ----------

  function collapse(row) {
    if (isNew(row)) {
      row.remove();
      recount();
      return;
    }
    row.querySelector("[data-board-detail]").hidden = true;
    row.querySelector("[data-board-action=expand]").setAttribute("aria-expanded", "false");
    row.classList.remove("is-open");
  }

  function expand(row) {
    for (const other of rows()) if (other !== row && other.classList.contains("is-open")) collapse(other);
    const form = row.querySelector("[data-board-edit]");
    const s = state(row);
    form.elements.room.value = s.room;
    form.elements.kind.value = s.kind;
    form.elements.summary.value = s.summary;
    form.elements.amount.value = s.amount;
    form.elements.currency.value = s.currency;
    showError(form, "");
    row.querySelector("[data-board-detail]").hidden = false;
    row.querySelector("[data-board-action=expand]").setAttribute("aria-expanded", "true");
    row.classList.add("is-open");
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
    const fresh = isNew(row);
    const texts = fresh ? ["внёс строку"] : changes(before, after);
    Object.assign(row.dataset, after);
    if (fresh) delete row.dataset.boardNew;
    render(row);
    collapse(row);
    if (texts.length) log(row, texts);
    flash(row);
    row.querySelector("[data-board-action=expand]").focus();
  }

  function toggle(row) {
    if (isNew(row)) return;
    const closing = row.dataset.status !== "closed";
    row.dataset.status = closing ? "closed" : "open";
    collapse(row);
    render(row);
    (closing ? closedList : openList).append(row);
    log(row, [closing ? "статус: открыт → закрыт" : "статус: закрыт → открыт"]);
    if (!closing) flash(row);
  }

  function addNote(row, form) {
    const text = form.elements.note.value.trim();
    if (!text) return;
    const time = clock(new Date());
    row.querySelector("[data-board-notes]").append(flash(clone("note", { login: me, time, text }), "is-new"));
    feedEntry(row, { login: me, time, text }, true);
    stamp(row, time);
    form.elements.note.value = "";
    recount();
  }

  board.addEventListener("click", (event) => {
    const button = event.target.closest("[data-board-action]");
    const row = button && button.closest("[data-board-row]");
    if (!row) return;
    const action = button.dataset.boardAction;
    if (action === "expand") {
      if (row.classList.contains("is-open")) collapse(row);
      else expand(row);
    } else if (action === "toggle") toggle(row);
    else if (action === "cancel") {
      const wasNew = isNew(row);
      collapse(row);
      (wasNew ? addButton : row.querySelector("[data-board-action=expand]")).focus();
    }
  });
  board.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = event.target;
    const row = form.closest("[data-board-row]");
    if (form.matches("[data-board-note-form]")) addNote(row, form);
    else if (form.matches("[data-board-edit]")) save(row, form);
  });
  board.addEventListener("keydown", (event) => {
    const row = event.key === "Escape" && event.target.closest(".board-row.is-open");
    if (!row) return;
    const wasNew = isNew(row);
    collapse(row);
    (wasNew ? addButton : row.querySelector("[data-board-action=expand]")).focus();
  });

  addButton.addEventListener("click", () => {
    const current = openList.querySelector("[data-board-new]");
    if (current) return current.querySelector("input[name=room]").focus();
    created += 1;
    const row = rowTemplate.firstElementChild.cloneNode(true);
    row.dataset.boardRow = `new${created}`;
    row.dataset.boardNew = "true";
    openList.append(row);
    expand(row);
    recount();
    row.querySelector("input[name=room]").focus();
  });

  feed.addEventListener("click", (event) => {
    const item = event.target.closest("[data-feed-row]");
    const row = item && board.querySelector(`[data-board-row="${CSS.escape(item.dataset.feedRow)}"]`);
    if (!row || row.closest("template")) return;
    const fold = row.closest("details");
    if (fold) fold.open = true;
    row.scrollIntoView({ behavior: motion ? "smooth" : "auto", block: "center" });
    flash(row);
  });
})();
