/* Страница 3D-витрины «как прототип» (spec 0069 §2.2, §5; #816, часть Б — #819): одна на
 * посетителя и кабинет.
 *
 * Настройки — JSON-блок #sr-config (язык, надписи, адреса сцены, помощника и просмотрщика,
 * предел группы). Разметку рисуют этот скрипт, `showroom_stage.js` (всё поверх модели) и
 * `showroom_panel.js` (вкладки справа) — только textContent: названия в сцене пишет отель.
 * 3D — `viewer.js` (часть А); его методы, которых может не быть, страница проверяет и без
 * них прячет кнопку.
 *
 * Стык для `assistant.js` и `request_form.js` — window.Showroom {viewer, scene, model, texts,
 * lang, textFor, audience(), slot(name), applyRooms(list), rooms(audience),
 * setRooms(audience, list), toggleRoom(audience, n), openRequest(kind), closeRequest(),
 * currentFloor()} и события document: "showroom:ready", "showroom:audience" (detail —
 * "group" | "booking"), "showroom:select" (номер или null), "showroom:rooms"
 * ({audience, rooms}). Слоты — [data-sr-slot=assistant|request].
 */
import { h, buildModel, plural } from "./showroom_core.js";
import { createStage } from "./showroom_stage.js";
import { createPanel } from "./showroom_panel.js";

const BOOKING_LIMIT = 3;

(async function () {
  "use strict";

  const configNode = document.getElementById("sr-config");
  const root = document.querySelector("[data-sr-root]");
  if (!configNode || !root) return;
  const cfg = JSON.parse(configNode.textContent);
  const t = cfg.texts;
  const lang = cfg.lang;
  root.classList.add("sr-root");

  const notice = h("p", { class: "sr-notice", role: "alert", hidden: true });
  root.appendChild(notice);
  let data;
  try {
    const response = await fetch(cfg.sceneUrl, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(String(response.status));
    data = await response.json();
  } catch (error) {
    notice.textContent = t.no_webgl;
    notice.hidden = false;
    return;
  }
  const scene = data.scene;
  const model = buildModel(scene, lang);
  const hasRequests = !!document.getElementById("showroom-request");

  // Состояние страницы — одно на всё: что выбрано, какая вкладка, что подобрано.
  const state = {
    tab: "group",
    audience: "group",
    group: [],
    short: 0,
    gsize: 30,
    found: [],
    booking: [],
    selRoom: null,
    selFloor: null,
    mode: "model",
    win: null,
    reqOpen: false,
    activeView: "home",
    hintSeen: false,
  };

  const stageNode = h("div", { class: "sr-stage" });
  const panelNode = h("aside", { class: "sr-panel" });
  root.appendChild(h("div", { class: "sr-main" }, [stageNode, panelNode]));
  root.appendChild(h("p", { class: "sr-foot", text: t.disclaimer }));
  const scrim = h("div", { class: "sr-scrim", hidden: true, onclick: () => closeRequest() });
  root.appendChild(scrim);

  // Цифры шапки — из сцены (страница посетителя; в кабинете места под них нет).
  const stats = document.querySelector("[data-sr-stats]");
  if (stats) {
    const roomsTotal = model.rooms.size;
    const items = [
      [scene.building.floors_total, t.stat_floors],
      [roomsTotal, t.stat_rooms],
      [model.saleFloors.length, t.stat_sale],
    ];
    stats.replaceChildren(...items.map(([n, forms]) => h("div", { class: "sr-stat" }, [h("b", { text: String(n) }), h("span", { text: plural(lang, n, forms) })])));
  }

  let viewer = null;
  const canvas = h("div", { class: "sr-canvas", role: "img", "aria-label": t.canvas_label });
  stageNode.appendChild(canvas);
  try {
    const { createViewer } = await import(cfg.viewerUrl);
    viewer = createViewer(canvas, scene, { textFor: model.textFor, labelInset: 56 });
  } catch (error) {
    canvas.appendChild(h("p", { class: "sr-notice", text: t.no_webgl }));
  }
  const can = (name) => !!viewer && typeof viewer[name] === "function";

  const highlighted = () => (state.audience === "group" ? state.group : [...new Set([...state.found, ...state.booking])]);
  // `fly: false` — номер добавлен или убран по одному: камера и выбранный номер остаются.
  function paint(fly) {
    if (viewer) viewer.highlight(highlighted(), { fly: fly !== false });
  }

  const ui = { render() {} };
  function set(patch) {
    Object.assign(state, patch);
    ui.render();
  }
  function emitRooms(audience) {
    document.dispatchEvent(new CustomEvent("showroom:rooms", { detail: { audience, rooms: rooms(audience) } }));
  }
  function rooms(audience) {
    return (audience === "booking" ? state.booking : state.group).slice();
  }
  function setRooms(audience, list, { fly = true, short = 0 } = {}) {
    const clean = [...new Set((list || []).map(String).filter((n) => model.rooms.has(n)))];
    if (audience === "booking") set({ booking: clean.slice(0, BOOKING_LIMIT) });
    else set({ group: clean.slice(0, cfg.maxGroupRooms || 60), short, selRoom: fly ? null : state.selRoom });
    paint(fly);
    emitRooms(audience);
  }
  function toggleRoom(audience, number) {
    const list = rooms(audience);
    const at = list.indexOf(number);
    if (at >= 0) list.splice(at, 1);
    else list.push(number);
    setRooms(audience, list, { fly: false, short: audience === "group" ? state.short : 0 });
  }
  function setTab(tab) {
    if (tab === state.tab) return;
    const audience = tab === "floors" ? state.audience : tab;
    const changed = audience !== state.audience;
    set({ tab, audience, reqOpen: false });
    if (viewer && can("setTapMode")) viewer.setTapMode(tab === "floors" ? "floor" : "room");
    if (changed) {
      paint(false);
      document.dispatchEvent(new CustomEvent("showroom:audience", { detail: audience }));
    }
  }
  function openRequest(kind) {
    if (!hasRequests) return;
    setTab(kind === "booking" ? "booking" : "group");
    set({ reqOpen: true });
  }
  function closeRequest() {
    if (state.reqOpen) set({ reqOpen: false });
  }
  function currentFloor() {
    if (state.win && state.mode !== "model") return state.win.floor;
    if (state.selFloor !== null) return state.selFloor;
    const room = state.selRoom && model.rooms.get(state.selRoom);
    return room ? room.floor : null;
  }

  const api = {
    viewer, scene, model, texts: t, lang, cfg, state, can, set, hasRequests,
    textFor: model.textFor,
    audience: () => state.audience,
    slot: (name) => root.querySelector(`[data-sr-slot="${name}"]`),
    rooms, setRooms, toggleRoom, setTab, openRequest, closeRequest, currentFloor,
    // Номера от помощника: у группы — это и есть группа, у гостя — подсветка, выбор — его.
    applyRooms(list) {
      const numbers = (list || []).map((room) => String(room.number ?? room));
      if (state.audience === "group") {
        if (state.tab !== "group") setTab("group");
        setRooms("group", numbers);
      } else {
        set({ found: numbers.filter((n) => model.rooms.has(n)) });
        paint(true);
      }
    },
  };

  const stage = createStage(stageNode, api);
  api.nav = stage.nav;
  const panel = createPanel(panelNode, api);
  ui.render = () => {
    root.classList.toggle("immersive", state.mode !== "model");
    root.classList.toggle("sheet", state.reqOpen);
    scrim.hidden = !state.reqOpen;
    stage.render();
    panel.render();
  };

  if (viewer) {
    viewer.on("select", (n) => {
      const number = n === null || n === undefined ? null : String(n);
      const room = number && model.rooms.get(number);
      set({ selRoom: number, selFloor: room && state.tab === "floors" ? room.floor : state.selFloor, hintSeen: true, activeView: number ? null : state.activeView });
      document.dispatchEvent(new CustomEvent("showroom:select", { detail: number }));
    });
    viewer.on("floor", (f) => set({ selFloor: f, activeView: null, hintSeen: true }));
    viewer.on("hover", (hit) => stage.hover(hit));
    viewer.on("mode", (m) => {
      const mode = m && m.mode ? m.mode : "model";
      set({ mode, win: mode === "model" ? null : { room: m.room ? String(m.room) : null, floor: m.floor } });
    });
  }

  window.Showroom = api;
  ui.render();
  document.dispatchEvent(new CustomEvent("showroom:ready", { detail: api }));
})();
