// Помощник 3D-витрины (spec 0069 §2.3, §5): фраза посетителя → `POST <страница>/navigate` →
// команды камеры и реплика. Каждая команда уходит событием `showroom:command` на document:
// камеру ведёт просмотрщик, карточку `draft_request` открывает форма запроса — блок не знает
// их устройства. Ответ модели выводится только textContent: реплику пишет модель.
"use strict";
(() => {
  const root = document.getElementById("showroom-assistant");
  if (!root) return;
  const T = JSON.parse(document.getElementById("showroom-assistant-texts").textContent);
  const input = root.querySelector("input[name=text]");
  const button = root.querySelector("button[type=submit]");
  const reply = root.querySelector(".assistant-reply");
  const base = location.pathname.replace(/\/+$/, "");
  const language = () => {
    const lang = (document.documentElement.lang || "en").slice(0, 2);
    return T.resting[lang] ? lang : "en";
  };
  const audience = () => (document.body.dataset.audience === "booking" ? "booking" : "group");
  const text = (key) => T[key][language()] || T[key].en;

  function relabel() {
    input.placeholder = text(audience() === "booking" ? "placeholder_booking" : "placeholder_group");
    button.textContent = text("button");
    root.querySelector(".assistant-hint").textContent = text("hint");
  }

  function say(message) {
    reply.textContent = message;
    reply.hidden = !message;
  }

  async function ask(phrase) {
    let response;
    try {
      response = await fetch(base + "/navigate", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({text: phrase, language: language(), audience: audience()}),
      });
    } catch (e) {
      return null;
    }
    if (!response.ok) return null;
    try { return await response.json(); } catch (e) { return null; }
  }

  root.addEventListener("submit", async (event) => {
    event.preventDefault();
    const phrase = input.value.trim();
    if (!phrase || button.disabled) return;
    button.disabled = true;
    const result = await ask(phrase);
    button.disabled = false;
    if (!result) { say(text("resting")); return; }
    for (const command of result.commands || []) {
      document.dispatchEvent(new CustomEvent("showroom:command", {detail: command}));
    }
    say(result.reply || "");
    input.value = "";
  });

  // Язык и вкладка «Для групп · Для гостей» меняются на странице без перезагрузки.
  document.addEventListener("showroom:relabel", relabel);
  new MutationObserver(relabel).observe(document.documentElement, {attributes: true, attributeFilter: ["lang"]});
  new MutationObserver(relabel).observe(document.body, {attributes: true, attributeFilter: ["data-audience"]});
  relabel();
})();
