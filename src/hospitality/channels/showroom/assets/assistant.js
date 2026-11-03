/* Помощник 3D-витрины (spec 0069 §2.3, §5): поле фразы в слоте [data-sr-slot=assistant].
 *
 * Фраза → POST cfg.navigateUrl (посетитель — /showroom/{slug}/navigate, кабинет —
 * /staff/{slug}/showroom/navigate) → {commands, reply, source}. Команды исполняет
 * просмотрщик через window.Showroom (showroom.js): focus_floor, focus_room, set_view,
 * step_floor, reset, find_rooms (номера уже подобрал сервер — applyRooms); черновик
 * draft_request уходит событием document "showroom:draft" — его карточку открывает
 * форма запроса (PR в). Реплику пишет модель: на страницу — только textContent.
 * Сеть, лимит или ошибка сервера — текст «Помощник отдыхает…», кнопки работают.
 */
(function () {
  "use strict";

  const configNode = document.getElementById("sr-config");
  if (!configNode) return;
  const cfg = JSON.parse(configNode.textContent);
  if (!cfg.navigateUrl) return;

  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "text") node.textContent = value;
      else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children || []) if (child) node.appendChild(child);
    return node;
  }

  function start(showroom) {
    const slot = showroom.slot("assistant");
    if (!slot || slot.dataset.ready) return;
    slot.dataset.ready = "1";
    const t = showroom.texts;
    const viewer = showroom.viewer;
    const floorOf = new Map();
    for (const floor of showroom.scene.floors) for (const room of floor.rooms) floorOf.set(room.number, floor.number);

    // «Выше» и «ниже» — от этажа на экране; его знает только страница: запоминаем этаж
    // каждого показа — кнопками линейки, касанием номера и командами помощника.
    let currentFloor = null;
    if (viewer) {
      const focusFloor = viewer.focusFloor;
      viewer.focusFloor = (n) => {
        const shown = focusFloor(n);
        if (shown) currentFloor = n;
        return shown;
      };
      viewer.on("select", (number) => {
        if (floorOf.has(number)) currentFloor = floorOf.get(number);
      });
      const reset = viewer.reset;
      viewer.reset = () => {
        currentFloor = null;
        return reset();
      };
    }

    const input = h("input", { type: "text", name: "text", class: "sr-input", maxlength: "300", enterkeyhint: "send", autocomplete: "off" });
    const button = h("button", { type: "submit", class: "sr-btn sr-btn-primary", text: t.show });
    const reply = h("p", { class: "sr-assistant-reply", role: "status", "aria-live": "polite", hidden: true });
    const form = h("form", { class: "sr-search" }, [input, button]);
    slot.appendChild(h("section", { class: "sr-card sr-assistant" }, [form, h("p", { class: "sr-hint", text: t.assistant_hint }), reply]));

    function relabel() {
      const placeholder = showroom.audience() === "booking" ? t.assistant_placeholder_booking : t.assistant_placeholder_group;
      input.placeholder = placeholder;
      input.setAttribute("aria-label", placeholder);
    }
    document.addEventListener("showroom:audience", relabel);
    relabel();

    function say(text) {
      reply.textContent = text || "";
      reply.hidden = !text;
    }

    function run(command) {
      switch (command.type) {
        case "focus_floor":
          if (viewer) viewer.focusFloor(command.floor);
          break;
        case "focus_room":
          if (viewer) viewer.focusRoom(command.room);
          break;
        case "set_view":
          if (viewer) viewer.setView(command.preset);
          break;
        case "step_floor":
          if (viewer && currentFloor !== null) viewer.focusFloor(currentFloor + (command.direction === "up" ? 1 : -1));
          break;
        case "reset":
          if (viewer) viewer.reset();
          break;
        case "find_rooms":
          showroom.applyRooms((command.rooms || []).map((number) => ({ number })));
          break;
        case "draft_request":
          document.dispatchEvent(new CustomEvent("showroom:draft", { detail: command }));
          break;
      }
    }

    async function ask(text) {
      try {
        const response = await fetch(cfg.navigateUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ text, language: showroom.lang, audience: showroom.audience() }),
        });
        return response.ok ? await response.json() : null;
      } catch (error) {
        return null;
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text || button.disabled) return;
      button.disabled = true;
      const result = await ask(text);
      button.disabled = false;
      if (!result) {
        say(t.assistant_resting);
        return;
      }
      (result.commands || []).forEach(run);
      say(result.reply);
      input.value = "";
    });
  }

  if (window.Showroom) start(window.Showroom);
  else document.addEventListener("showroom:ready", (event) => start(event.detail), { once: true });
})();
