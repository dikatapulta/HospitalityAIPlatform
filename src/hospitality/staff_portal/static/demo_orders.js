// Демо «Заказы в номер» (issue #438): статус меняется только в браузере —
// ни запросов к серверу, ни записи. Разметку не сочиняет: переключает атрибут
// hidden у элементов с data-when по стадии карточки (data-stage) и пишет в
// строку шагов надпись нажатой кнопки со временем. Фильтр служб — те же чипсы,
// что у очереди, но кнопками: ссылка перезагрузила бы страницу и сбросила демо.
"use strict";

(() => {
  const cards = Array.from(document.querySelectorAll("[data-demo-order]"));

  function showStage(card, stage) {
    card.dataset.stage = stage;
    for (const el of card.querySelectorAll("[data-when]")) {
      el.hidden = el.dataset.when !== stage;
    }
  }

  for (const card of cards) {
    const trail = card.querySelector("[data-demo-trail]");
    for (const button of card.querySelectorAll("[data-demo-next]")) {
      button.addEventListener("click", () => {
        const time = new Date().toTimeString().slice(0, 5);
        trail.textContent = (trail.textContent ? trail.textContent + " · " : "") +
          button.textContent.trim() + " " + time;
        trail.hidden = false;
        showStage(card, button.dataset.demoNext);
      });
    }
  }

  const chips = Array.from(document.querySelectorAll("[data-demo-filter]"));
  for (const chip of chips) {
    chip.addEventListener("click", () => {
      const filter = chip.dataset.demoFilter;
      for (const other of chips) other.classList.toggle("chip-active", other === chip);
      for (const card of cards) {
        card.hidden = filter !== "all" && card.dataset.section !== filter;
      }
    });
  }
})();
