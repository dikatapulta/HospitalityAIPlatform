/* Страница 3D-витрины (spec 0069 §2.2, §5): одна на посетителя и кабинет.
 *
 * Настройки — JSON-блок #sr-config (язык, надписи, адреса сцены, подбора и
 * просмотрщика): страница посетителя ходит в /showroom/{slug}/…, кабинет — в
 * /staff/{slug}/showroom/… под своей сессией. Разметку рисует этот скрипт —
 * только textContent, без innerHTML: названия в сцене пишет отель.
 *
 * Для PR (б) и (в): после загрузки — window.Showroom {viewer, scene, texts, lang,
 * audience(), slot(name), applyRooms(list)} и событие document "showroom:ready";
 * смена вкладки — "showroom:audience" (detail — "group" | "booking"), выбор номера —
 * "showroom:select" (detail — номер). Слоты для блоков — [data-sr-slot=assistant|request].
 */
(async function () {
  "use strict";

  const configNode = document.getElementById("sr-config");
  const root = document.querySelector("[data-sr-root]");
  if (!configNode || !root) return;
  const cfg = JSON.parse(configNode.textContent);
  const t = cfg.texts;
  const lang = cfg.lang;
  const textFor = (value) => (value ? value[lang] || value.en : "");
  const fill = (template, vars) => template.replace(/\{(\w+)\}/g, (_, key) => String(vars[key] ?? ""));

  function h(tag, attrs, children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "text") node.textContent = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children || []) if (child) node.appendChild(child);
    return node;
  }

  const notice = h("p", { class: "sr-notice", role: "alert", hidden: true });
  root.appendChild(notice);
  function fail(text) {
    notice.textContent = text;
    notice.hidden = false;
  }

  let data;
  try {
    const response = await fetch(cfg.sceneUrl, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(String(response.status));
    data = await response.json();
  } catch (error) {
    fail(t.no_webgl);
    return;
  }
  const scene = data.scene;
  const roomTypeName = (key) => textFor((scene.room_types[key] || {}).names) || key;

  // Вкладки «Для групп · Для гостей» (§2.2): различаются только блоки форм и помощника.
  root.dataset.audience = "group";
  const tabs = h("div", { class: "sr-audience", role: "group" });
  const tabButtons = ["group", "booking"].map((audience) =>
    h("button", {
      type: "button",
      class: "sr-tab",
      "aria-pressed": audience === "group" ? "true" : "false",
      text: t[`audience_${audience}`],
      onclick: () => {
        root.dataset.audience = audience;
        tabButtons.forEach((b) => b.setAttribute("aria-pressed", b === tabButtons[audience === "group" ? 0 : 1] ? "true" : "false"));
        document.dispatchEvent(new CustomEvent("showroom:audience", { detail: audience }));
      },
    })
  );
  tabButtons.forEach((b) => tabs.appendChild(b));

  const canvas = h("div", { class: "sr-canvas" });
  const viewButtons = h("div", { class: "sr-views" }, [
    ...["mountains", "city", "top", "street"].map((preset) =>
      h("button", { type: "button", class: "sr-chip", text: t[`view_${preset}`], onclick: () => showView(preset) })
    ),
    h("button", { type: "button", class: "sr-chip sr-chip-quiet", text: t.reset, onclick: () => viewer && viewer.reset() }),
  ]);
  const stage = h("section", { class: "sr-stage" }, [canvas, viewButtons, h("p", { class: "sr-disclaimer", text: t.disclaimer })]);

  // Поиск номера.
  const searchInput = h("input", { type: "search", class: "sr-input", placeholder: t.room_search, "aria-label": t.room_search, maxlength: "10" });
  const searchError = h("p", { class: "sr-hint sr-error", hidden: true, role: "status" });
  const search = h("form", { class: "sr-search", onsubmit: (e) => {
    e.preventDefault();
    const ok = viewer && viewer.focusRoom(searchInput.value.trim());
    searchError.textContent = ok ? "" : t.room_not_found;
    searchError.hidden = !!ok;
  } }, [searchInput, h("button", { type: "submit", class: "sr-btn", text: t.find })]);

  // Линейка этажей: этажи сцены сверху вниз.
  const floors = h("div", { class: "sr-floors", role: "group", "aria-label": t.floors });
  [...scene.floors].sort((a, b) => b.number - a.number).forEach((floor) => {
    const label = floor.label ? `${floor.number} · ${textFor(floor.label)}` : fill(t.floor, { n: floor.number });
    floors.appendChild(h("button", { type: "button", class: "sr-floor", text: label, onclick: () => viewer && viewer.focusFloor(floor.number) }));
  });

  // Панель подбора (§5) — то же, что умеет помощник: find_rooms на сервере.
  const select = (name, options, anyText) =>
    h("select", { name, class: "sr-input" }, [h("option", { value: "", text: anyText || t.any }), ...options.map(([value, text]) => h("option", { value, text }))]);
  const field = (label, control) => h("label", { class: "sr-field" }, [h("span", { text: label }), control]);
  const number = (name, max) => h("input", { type: "number", name, min: "1", max: String(max), inputmode: "numeric", class: "sr-input" });
  const sides = Object.entries(scene.sides).map(([key, side]) => [key, textFor(side.names)]);
  const types = Object.entries(scene.room_types).map(([key, type]) => [key, textFor(type.names)]);
  const views = Object.entries(scene.views).map(([key, view]) => [key, textFor(view.names)]);
  const result = h("p", { class: "sr-hint", role: "status" });
  const chips = h("div", { class: "sr-chips" });
  const panel = h("form", { class: "sr-panel", onsubmit: (e) => { e.preventDefault(); runQuery(new FormData(panel)); } }, [
    h("h2", { class: "sr-h2", text: t.panel_title }),
    h("div", { class: "sr-row" }, [field(t.floor_from, number("floor_from", scene.building.floors_total)), field(t.floor_to, number("floor_to", scene.building.floors_total))]),
    field(t.side, select("side", sides, t.any_side)),
    field(t.room_type, select("type_key", types)),
    field(t.view, select("view_key", views)),
    field(t.count, number("count", 60)),
    h("button", { type: "submit", class: "sr-btn sr-btn-primary", text: t.show }),
    result,
    chips,
  ]);

  const card = h("section", { class: "sr-card", hidden: true, "aria-live": "polite" });
  const side = h("aside", { class: "sr-side" }, [
    // Помощник — первым: фраза словами и есть главный способ подобрать номера (spec 0069 §2.3).
    h("div", { "data-sr-slot": "assistant" }),
    search, searchError, floors, card, panel,
    h("div", { "data-sr-slot": "request" }),
  ]);
  root.appendChild(tabs);
  root.appendChild(h("div", { class: "sr-main" }, [stage, side]));

  function showCard(numberValue) {
    let found = null;
    for (const floor of scene.floors) for (const room of floor.rooms) if (room.number === numberValue) found = { floor, room };
    card.replaceChildren();
    if (!found) { card.hidden = true; return; }
    const { floor, room } = found;
    const type = scene.room_types[room.type_key] || {};
    const roomViews = room.view_keys.length ? room.view_keys : (scene.sides[room.side] || {}).view_keys || [];
    card.append(
      h("h2", { class: "sr-h2", text: fill(t.room, { number: room.number, floor: floor.number }) }),
      h("p", { text: roomTypeName(room.type_key) + (type.note ? ` — ${textFor(type.note)}` : "") }),
      h("p", { class: "sr-hint", text: `${t.side}: ${textFor((scene.sides[room.side] || {}).names)}` }),
      roomViews.length ? h("p", { class: "sr-hint", text: `${t.view}: ${roomViews.map((k) => textFor((scene.views[k] || {}).names) || k).join(", ")}` }) : null,
      h("p", { class: "sr-price", text: type.price_note ? fill(t.price_note, { price: textFor(type.price_note) }) : t.no_price })
    );
    card.hidden = false;
  }

  function applyRooms(list) {
    const numbers = list.map((room) => room.number);
    if (viewer) viewer.highlight(numbers);
    result.textContent = numbers.length ? fill(t.matches, { n: numbers.length }) : t.no_matches;
    chips.replaceChildren(...numbers.map((n) => h("button", { type: "button", class: "sr-chip", text: n, onclick: () => viewer && viewer.focusRoom(n) })));
  }

  async function runQuery(form) {
    const params = new URLSearchParams();
    for (const [key, value] of form.entries()) if (String(value).trim()) params.set(key, String(value).trim());
    try {
      const response = await fetch(`${cfg.roomsUrl}?${params}`, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      applyRooms((await response.json()).rooms);
    } catch (error) {
      result.textContent = t.no_matches;
    }
  }

  function showView(preset) {
    if (!viewer) return;
    viewer.setView(preset);
    if (scene.views[preset]) {
      const form = new FormData();
      form.set("view_key", preset);
      runQuery(form);
    }
  }

  let viewer = null;
  try {
    const { createViewer } = await import(cfg.viewerUrl);
    viewer = createViewer(canvas, scene, { textFor });
    viewer.on("select", (n) => {
      showCard(n);
      document.dispatchEvent(new CustomEvent("showroom:select", { detail: n }));
    });
  } catch (error) {
    canvas.appendChild(h("p", { class: "sr-notice", text: t.no_webgl }));
  }

  window.Showroom = {
    viewer,
    scene,
    texts: t,
    lang,
    textFor,
    applyRooms,
    audience: () => root.dataset.audience,
    slot: (name) => root.querySelector(`[data-sr-slot="${name}"]`),
  };
  document.dispatchEvent(new CustomEvent("showroom:ready", { detail: window.Showroom }));
})();
