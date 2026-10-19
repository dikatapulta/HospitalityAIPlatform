// Демо «Передача смены» (issue #450): касса к сдаче смены одним кликом — ни
// запросов к серверу, ни записи. Разметку не сочиняет: по «Подготовить кассу»
// по очереди переводит шаги источников в data-state load → done и ставит класс
// is-filled полям бланка с data-src этого источника; переключатель примера
// смены — атрибут hidden у data-detail и data-handover-sheet. Пишет только
// замер «Касса готова за N,N сек» (от нажатия до последнего ✓), дату и смену
// по часам компьютера на стойке.
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
  let scenario = options[0].dataset.handoverScenario;
  let running = false;

  // Дата — день, когда сдают смену; смена — та, что идёт сейчас (как на бумаге).
  const now = new Date();
  const date = now.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
  const shift = now.getHours() >= 8 && now.getHours() < 20 ? "08:00–20:00" : "20:00–08:00";
  for (const el of root.querySelectorAll("[data-handover-date]")) el.textContent = date;
  for (const el of root.querySelectorAll("[data-handover-shift]")) el.textContent = shift;

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const setProgress = (done) => progress.style.setProperty("--done", String(done / steps.length));

  function reset() {
    for (const step of steps) step.dataset.state = "wait";
    for (const el of root.querySelectorAll(".is-filled")) el.classList.remove("is-filled");
    setProgress(0);
    result.hidden = true;
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
    for (const option of options) option.disabled = false;
    running = false;
  }

  prepare.addEventListener("click", () => {
    if (!running) run();
  });
  for (const option of options) {
    option.addEventListener("click", () => {
      if (!running) choose(option.dataset.handoverScenario);
    });
  }
  root.querySelector("[data-handover-print]").addEventListener("click", () => window.print());
})();
