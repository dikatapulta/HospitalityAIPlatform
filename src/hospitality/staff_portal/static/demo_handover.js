// Демо «Передача смены» (issue #450): касса к сдаче смены одним кликом — ни
// запросов к серверу, ни записи. Разметку не сочиняет: по «Подготовить кассу»
// по очереди переводит шаги источников в data-state load → done и ставит класс
// is-filled полям бланка с data-src этого источника; переключатель примера
// смены — атрибут hidden у data-detail и data-handover-sheet. Пишет только
// замер «Касса готова за N,N сек» (от нажатия до последнего ✓), дату и смену
// по часам компьютера на стойке.
//
// «Ручной режим» — бланк как Excel-образец: поля data-in становятся
// редактируемыми и пустыми, data-calc и data-check пересчитываются на каждый
// ввод по формулам из атрибутов шаблона (_handover_sheet.html). Суммы — в
// тиынах целыми числами, без ошибок округления дробей. Выход из режима
// возвращает пример. data-zero (возвраты, #469): пустое поле стоит нулём — в
// фокусе ноль стирается под ввод, ушли пустым — ноль возвращается.
//
// data-handover-manual-only (#469, гостевая копия): кнопок примера и «Ручного
// режима» на странице нет — ручной режим включён сразу и навсегда.
"use strict";

(() => {
  const root = document.querySelector("[data-handover]");
  if (!root) return;
  const steps = Array.from(root.querySelectorAll("[data-source]"));
  const options = Array.from(root.querySelectorAll("[data-handover-scenario]"));
  const sheets = Array.from(root.querySelectorAll("[data-handover-sheet]"));
  const prepare = root.querySelector("[data-handover-prepare]");
  const progress = root.querySelector("[data-handover-progress]");
  const result = root.querySelector("[data-handover-result]");
  const elapsed = root.querySelector("[data-handover-elapsed]");
  const manualToggle = root.querySelector("[data-handover-manual]");
  const manualPanel = root.querySelector("[data-handover-manual-panel]");
  const manualOnly = root.hasAttribute("data-handover-manual-only");
  let scenario = options[0].dataset.handoverScenario;
  let running = false;
  let manual = false;

  // Дата — день, когда сдают смену; смена — та, что идёт сейчас (как на бумаге).
  const now = new Date();
  const date = now.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
  const shift = now.getHours() >= 8 && now.getHours() < 20 ? "08:00–20:00" : "20:00–08:00";
  for (const el of root.querySelectorAll("[data-handover-date]")) el.textContent = date;
  for (const el of root.querySelectorAll("[data-handover-shift]")) el.textContent = shift;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const setProgress = (done) => progress?.style.setProperty("--done", String(done / steps.length));

  function reset() {
    for (const step of steps) step.dataset.state = "wait";
    for (const el of root.querySelectorAll(".is-filled")) el.classList.remove("is-filled");
    setProgress(0);
    if (result) result.hidden = true;
  }

  function choose(key) {
    scenario = key;
    for (const option of options) {
      option.setAttribute("aria-pressed", String(option.dataset.handoverScenario === key));
    }
    for (const el of root.querySelectorAll("[data-detail]")) el.hidden = el.dataset.detail !== key;
    for (const sheet of sheets) sheet.hidden = sheet.dataset.handoverSheet !== key;
    reset();
  }

  async function run() {
    running = true;
    reset();
    prepare.disabled = true;
    manualToggle.disabled = true;
    for (const option of options) option.disabled = true;
    const sheet = sheets.find((candidate) => candidate.dataset.handoverSheet === scenario);
    const started = performance.now();
    for (const [index, step] of steps.entries()) {
      step.dataset.state = "load";
      await sleep(600 + Math.random() * 200);
      step.dataset.state = "done";
      for (const field of sheet.querySelectorAll(`[data-src="${step.dataset.source}"]`)) {
        field.classList.add("is-filled");
      }
      setProgress(index + 1);
    }
    const seconds = (performance.now() - started) / 1000;
    elapsed.textContent = seconds.toFixed(1).replace(".", ",");
    result.hidden = false;
    prepare.disabled = false;
    manualToggle.disabled = false;
    for (const option of options) option.disabled = false;
    running = false;
  }

  // ---------- Ручной режим ----------

  const NBSP = " ";
  const group = (digits) => digits.replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
  // Значение — в сотых долях: тиыны у сумм, сотые у штук (5 шт. = 500).
  const FORMATS = {
    amount: (v) => (v < 0 ? "−" : "") + group(String(Math.floor(Math.abs(v) / 100))) + ","
      + String(Math.abs(v) % 100).padStart(2, "0"),
    tg: (v) => FORMATS.amount(v) + NBSP + "тг",
    whole: (v) => (v < 0 ? "−" : "") + group(String(Math.round(Math.abs(v) / 100))),
    count: (v) => String(Math.round(v / 100)),
    pieces: (v) => FORMATS.whole(v) + NBSP + "шт.",
  };
  const signedTg = (v) => (v > 0 ? "+" : "−") + FORMATS.tg(Math.abs(v));

  const zero = (el) => FORMATS[el.dataset.format](0);

  // «484667,19», «1 024 300», «733400 тг» → 48466719; пусто → null; мусор → NaN.
  function parse(el) {
    const text = el.textContent.replace(/[\s  ]|тг|₸/g, "").replace(",", ".");
    if (text === "") return null;
    const pattern = el.dataset.format === "count" ? /^\d+$/ : /^\d+(\.\d{1,2})?$/;
    return pattern.test(text) ? Math.round(Number(text) * 100) : NaN;
  }

  function evaluate(sheet) {
    const memo = new Map();
    const value = (name) => {
      if (memo.has(name)) return memo.get(name);
      memo.set(name, null); // защита от цикла в формулах шаблона
      const el = sheet.querySelector(`[data-in="${name}"], [data-cell="${name}"]`);
      let v = null;
      if (el && el.dataset.in !== undefined) {
        const parsed = parse(el);
        // Ноль по умолчанию — не ввод: итог пустого бланка остаётся пустым.
        v = Number.isNaN(parsed) || (parsed === 0 && el.dataset.zero !== undefined) ? null : parsed;
      } else if (el) {
        v = formula(el.dataset.calc);
      }
      memo.set(name, v);
      return v;
    };
    // Пустое слагаемое — ноль; результат пуст, только если пусто всё.
    const formula = (expression) => {
      let total = 0;
      let any = false;
      for (const term of expression.trim().split(/\s+/)) {
        const [, sign, factor, name] = term.match(/^(-?)(?:(\d+)\*)?(\w+)$/);
        const v = value(name);
        if (v === null) continue;
        any = true;
        total += (sign ? -1 : 1) * Number(factor || 1) * v;
      }
      return any ? total : null;
    };
    for (const el of sheet.querySelectorAll("[data-calc]")) {
      const v = formula(el.dataset.calc);
      el.textContent = v === null ? "" : FORMATS[el.dataset.format](v);
    }
    for (const el of sheet.querySelectorAll("[data-check]")) {
      const ready = el.dataset.need.split(/\s+/).every((name) => value(name) !== null);
      const v = ready ? formula(el.dataset.check) : null;
      el.classList.toggle("is-filled", v !== null);
      el.classList.toggle("sheet-check-ok", v === 0);
      el.classList.toggle("sheet-check-bad", v !== null && v !== 0);
      if (v === null) el.textContent = "";
      else if (v === 0) el.textContent = el.dataset.ok;
      else el.textContent = el.dataset[v > 0 ? "plus" : "minus"].replace("{}", signedTg(v));
    }
  }

  const inputs = () => sheets.flatMap((sheet) => Array.from(sheet.querySelectorAll("[data-in]")));
  // Пример с сервера — то, что затирает ручной режим и возвращает выход из него.
  const demoFields = () => root.querySelectorAll("[data-in][data-src], [data-calc], [data-check]");

  function clearSheets() {
    for (const el of inputs()) {
      el.classList.remove("is-invalid");
      if (el.dataset.src !== undefined) el.textContent = el.dataset.zero !== undefined ? zero(el) : "";
    }
    for (const sheet of sheets) evaluate(sheet);
  }

  function setManual(on, focus = true) {
    manual = on;
    reset();
    root.classList.toggle("is-manual", on);
    manualPanel.hidden = !on;
    if (manualToggle) {
      manualToggle.setAttribute("aria-pressed", String(on));
      manualToggle.textContent = on ? "Выйти из ручного режима" : "Ручной режим";
    }
    for (const el of inputs()) {
      if (on) {
        el.setAttribute("contenteditable", "plaintext-only");
        el.setAttribute("role", "textbox");
        if (el.dataset.kind !== "text") el.setAttribute("inputmode", "decimal");
      } else {
        for (const name of ["contenteditable", "role", "inputmode"]) el.removeAttribute(name);
        el.classList.remove("is-invalid");
      }
    }
    for (const el of demoFields()) {
      if (on) {
        el.dataset.demoText = el.textContent;
        el.dataset.demoClass = el.className;
      } else {
        el.textContent = el.dataset.demoText;
        el.className = el.dataset.demoClass;
      }
    }
    if (on) {
      clearSheets();
      if (focus) sheets.find((sheet) => !sheet.hidden).querySelector("[data-in][data-src]").focus();
    }
  }

  root.addEventListener("input", (event) => {
    const sheet = event.target.closest("[data-handover-sheet]");
    if (manual && sheet) evaluate(sheet);
  });
  // Вошли в поле с нулём по умолчанию — ноль стирается, вписывать поверх не надо.
  root.addEventListener("focusin", (event) => {
    const el = event.target;
    if (manual && el.dataset.zero !== undefined && parse(el) === 0) el.textContent = "";
  });
  // Ушли из поля — сумма записывается как на бланке («484 667,19 тг»), мусор —
  // красным, пустой возврат — снова нулём.
  root.addEventListener("focusout", (event) => {
    const el = event.target;
    if (!manual || el.dataset.in === undefined || el.dataset.kind === "text") return;
    const v = parse(el);
    el.classList.toggle("is-invalid", Number.isNaN(v));
    if (v === null && el.dataset.zero !== undefined) el.textContent = zero(el);
    else if (v !== null && !Number.isNaN(v)) el.textContent = FORMATS[el.dataset.format](v);
  });
  // Enter — к следующему полю, как Tab в Excel-образце (в примечании — тоже).
  root.addEventListener("keydown", (event) => {
    const el = event.target;
    if (!manual || event.key !== "Enter" || el.dataset.in === undefined) return;
    event.preventDefault();
    const fields = Array.from(el.closest("[data-handover-sheet]").querySelectorAll("[data-in]"));
    const next = fields[fields.indexOf(el) + 1];
    if (next) next.focus();
    else el.blur();
  });

  prepare?.addEventListener("click", () => {
    if (!running) run();
  });
  manualToggle?.addEventListener("click", () => {
    if (!running) setManual(!manual);
  });
  root.querySelector("[data-handover-clear]").addEventListener("click", clearSheets);
  for (const option of options) {
    option.addEventListener("click", () => {
      if (!running) choose(option.dataset.handoverScenario);
    });
  }
  for (const button of root.querySelectorAll("[data-handover-print]")) {
    button.addEventListener("click", () => {
      document.activeElement.blur();
      window.print();
    });
  }
  if (manualOnly) setManual(true, false);
})();
