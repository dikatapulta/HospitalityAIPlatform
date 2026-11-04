/* Панель справа 3D-витрины (#816, часть Б): вкладки «Для групп · Для гостей · Этажи».
 *
 * «Для групп» — число человек → подбор прототипа (`pickGroup`), список по этажам, «Сбросить»,
 * «Запросить эти номера» — форма `request_form.js` встаёт в слот [data-sr-slot=request].
 * «Для гостей» — та же форма для запроса на бронирование. «Этажи» — кнопки этажей и план
 * этажа: номера по сторонам в порядке сцены (раскладка — как у просмотрщика, если он её
 * отдаёт, `floorLayout`). На телефоне открытая форма — нижняя шторка (класс `sheet`).
 */
import { h, svg, fill, plural, ranges, pickGroup, groupSummary } from "./showroom_core.js";

export function createPanel(panel, S) {
  const { texts: t, model, lang, state, viewer, can } = S;
  const floorN = (n) => fill(t.floor_n, { n });
  const tabsList = ["group", ...(S.hasRequests ? ["booking"] : []), "floors"];
  const tabLabel = { group: t.audience_group, booking: t.audience_booking, floors: t.tab_floors };
  const tabButtons = tabsList.map((key) => h("button", { type: "button", role: "tab", class: "sr-tab", "data-tab": key, text: tabLabel[key], onclick: () => S.setTab(key) }));
  const tabs = h("div", { class: "sr-tabs", role: "tablist", style: `grid-template-columns:repeat(${tabsList.length},minmax(0,1fr))` }, tabButtons);
  const pane = h("div", { class: "sr-pane", "aria-live": "polite" });
  panel.append(h("div", { class: "sr-tabbar" }, [tabs]), pane);

  const goRoom = (n) => S.nav && S.nav.goRoom(n);
  const requestSlot = () => h("div", { class: "sr-request-slot", "data-sr-slot": "request" });

  // ---------- «Для групп» ----------
  function pick(n) {
    const size = Math.max(1, Math.min(Number(n) || 0, S.cfg.maxGroupRooms || 60));
    const res = pickGroup(model, size);
    S.set({ gsize: size });
    S.setRooms("group", res.rooms, { short: res.short });
  }
  function plaque() {
    const sum = groupSummary(model, state.group, t, lang);
    const list = h("div", { class: "sr-glist" }, sum.floors.map((f) => h("div", { class: "sr-gfloor" }, [
      h("span", { class: "sr-gfl", text: floorN(f) }),
      h("div", { class: "sr-gchips" }, sum.rooms.filter((r) => r.floor === f).map((r) =>
        h("button", { type: "button", class: `sr-rchip${r.lead ? " m" : ""}${state.selRoom === r.number ? " on" : ""}`,
          "aria-label": `${fill(t.room_n, { n: r.number })}, ${model.sideName(r.side)}`, text: r.number, onclick: () => goRoom(r.number) }))),
    ])));
    return h("div", { class: "sr-plaque" }, [
      h("p", { class: "sr-ptitle" }, sum.parts.flatMap((part, i) => [i ? " · " : null, h("span", { class: "sr-nw", text: part })])),
      state.short ? h("p", { class: "sr-warn", text: fill(t.not_enough, { n: state.short, rooms: plural(lang, state.short, t.stat_rooms) }) }) : null,
      list,
      S.hasRequests ? h("button", { type: "button", class: "sr-btn primary sr-reqcta", text: t.req_open, onclick: () => S.openRequest("group") }) : null,
      h("p", { class: "sr-small sr-muted", text: t.edit_hint }),
      h("div", { class: "sr-prow" }, [
        model.leadSide ? h("span", { class: "sr-keys" }, [h("span", {}, [h("i", { class: "sr-key-lead" }), model.sideName(model.leadSide)])]) : h("span"),
        h("button", { type: "button", class: "sr-btn", text: t.group_reset, onclick: () => S.setRooms("group", [], { fly: false }) }),
      ]),
    ]);
  }
  function groupPane() {
    if (state.reqOpen) return [requestSlot()];
    const size = h("input", { id: "sr-gsize", class: "sr-num", type: "number", inputmode: "numeric", min: "1", max: String(S.cfg.maxGroupRooms || 60), value: String(state.gsize) });
    const form = h("form", { class: "sr-gform", novalidate: true, onsubmit: (e) => { e.preventDefault(); pick(size.value); } }, [
      h("label", { for: "sr-gsize", text: t.group_size }),
      h("div", { class: "sr-grow" }, [size, h("button", { type: "submit", class: state.group.length ? "sr-btn" : "sr-btn primary", text: t.pick })]),
      h("p", { class: "sr-small sr-muted", text: model.leadSide ? fill(t.group_note, { side: model.sideName(model.leadSide) }) : t.group_note_plain }),
    ]);
    const head = [h("h2", { text: t.groups_title }), h("p", { class: "sr-muted", text: fill(t.groups_intro, { list: ranges(model.saleFloors) }) })];
    return [
      ...(state.group.length ? [plaque(), form] : [...head, form]),
      h("div", { class: "sr-keys" }, [h("span", {}, [h("i", { class: "sr-key-gold" }), t.key_group])]),
      h("p", { class: "sr-demo", text: t.sales_note }),
    ];
  }

  // ---------- «Для гостей» ----------
  function bookingPane() {
    // Заголовок — у самой формы («Запрос на бронирование»), здесь — только как выбрать номер.
    return [h("p", { class: "sr-muted", text: t.booking_intro }), requestSlot()];
  }

  // ---------- «Этажи» ----------
  function floorButtons() {
    const box = h("div", { class: "sr-floors", role: "group", "aria-label": t.floor_label });
    for (let f = 1; f <= S.scene.building.floors_total; f += 1) {
      const sale = model.isSale(f);
      const note = sale ? null : model.floorLabel(f) || t.no_plan;
      box.appendChild(h("button", { type: "button", class: sale ? "sr-fbtn" : "sr-fbtn nodata", "aria-pressed": String(state.selFloor === f), title: note, "aria-label": note ? `${f}: ${note}` : null, text: String(f), onclick: () => S.nav && S.nav.goFloor(f) }));
    }
    return box;
  }
  function usesList() {
    // «3–4 — Технический этаж»: подряд идущие этажи с одной подписью — одной строкой.
    const rows = [];
    for (let f = 1; f <= S.scene.building.floors_total; f += 1) {
      const label = model.isSale(f) ? "" : model.floorLabel(f);
      const last = rows[rows.length - 1];
      if (label && last && last.label === label && last.to === f - 1) last.to = f;
      else if (label) rows.push({ from: f, to: f, label });
    }
    return h("ul", { class: "sr-uses" }, rows.map((r) => h("li", { text: `${r.from === r.to ? r.from : `${r.from}–${r.to}`} — ${r.label}` })));
  }
  function layout(f) {
    // Раскладка просмотрщика, если он её отдаёт; иначе — номера стороны в порядке сцены.
    if (can("floorLayout")) {
      const entries = viewer.floorLayout(f);
      if (entries && entries.length) return entries;
    }
    const teeth = Math.max(1, ...S.scene.floors.flatMap((fl) => ["north", "south", "east", "west"].map((s) => fl.rooms.filter((r) => r.side === s).length)));
    const out = [];
    const floor = model.floors.get(f);
    for (const side of Object.keys(S.scene.sides)) {
      const list = floor.rooms.filter((r) => r.side === side);
      let extra = teeth - list.length;
      const spans = list.map((r) => ((S.scene.room_types[r.type_key] || {}).windows === 2 && extra-- > 0 ? 2 : 1));
      let k = teeth - spans.reduce((a, b) => a + b, 0);
      list.forEach((r, i) => { out.push({ number: r.number, side, a: k, b: k + spans[i] - 1, teeth }); k += spans[i]; });
    }
    return out;
  }
  function planSvg(f) {
    const entries = layout(f);
    const teeth = Math.max(1, ...entries.map((e) => (e.teeth || e.b + 1)));
    const sides = [...new Set(entries.map((e) => e.side))];
    // Ведущая сторона — справа, как в прототипе; остальные — слева.
    const right = sides.includes(model.leadSide) ? model.leadSide : sides[0];
    const left = sides.find((s) => s !== right) || null;
    const y0 = 70;
    const sh = 456 / teeth;
    const cols = { [left]: { x: 66, w: 60 }, [right]: { x: 174, w: 60 } };
    const textAttrs = { "text-anchor": "middle", "font-size": 11, fill: "#8ea9b4" };
    const body = [
      left ? svg("text", { x: 96, y: 22, ...textAttrs, text: model.sideName(left) }) : null,
      svg("text", { x: 204, y: 22, ...textAttrs, text: model.sideName(right) }),
      svg("g", { fill: "none", stroke: "#3a5a67", "stroke-width": 1.5 }, [
        svg("path", { d: `M62 ${y0} Q150 4 238 ${y0}` }),
        svg("path", { d: `M62 ${y0 + 456} Q150 596 238 ${y0 + 456}` }),
        svg("path", { d: `M62 ${y0} V${y0 + 456} M238 ${y0} V${y0 + 456}` }),
      ]),
      svg("rect", { x: 136, y: y0, width: 28, height: 456, rx: 4, fill: "#1c3745" }),
    ];
    for (const e of entries) {
      const col = cols[e.side];
      if (!col) continue;
      const room = model.rooms.get(e.number);
      if (!room) continue;
      const y = y0 + e.a * sh + 2;
      const hgt = (e.b - e.a + 1) * sh - 4;
      const sel = state.selRoom === e.number;
      const inGroup = S.rooms(state.audience).includes(e.number);
      const label = `${e.number} · ${model.typeName(room.type_key)} · ${model.sideName(room.side)}${inGroup ? ` · ${state.audience === "group" ? t.key_group : t.key_booking}` : ""}`;
      const g = svg("g", { class: "sr-room", role: "button", tabindex: 0, "aria-label": label }, [
        svg("rect", { x: col.x, y, width: col.w, height: hgt, rx: 5, fill: inGroup ? "#f2b705" : "#f4efe6", stroke: sel ? "#ffffff" : "none", "stroke-width": sel ? 3 : 0 }),
        svg("text", { x: col.x + col.w / 2, y: y + hgt / 2 + 4, "text-anchor": "middle", "font-size": 12, "font-weight": 700, fill: "#0a1a20", text: e.number }),
      ]);
      const choose = () => viewer && viewer.select(e.number);
      g.addEventListener("click", choose);
      g.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); choose(); }
      });
      body.push(g);
    }
    return svg("svg", { class: "sr-plan", viewBox: "0 0 300 600", role: "group", "aria-label": fill(t.plan_aria, { n: f }) }, body);
  }
  function floorsPane() {
    const f = state.selFloor;
    if (f === null) return [h("h2", { text: t.all_hotel }), h("p", { class: "sr-muted", text: t.floors_intro }), floorButtons(), usesList()];
    const total = S.scene.building.floors_total;
    const step = (to, label, arrow) => h("button", { type: "button", class: "sr-btn", disabled: to < 1 || to > total, "aria-label": label, title: label, text: arrow, onclick: () => S.nav && S.nav.goFloor(to) });
    const head = h("div", { class: "sr-row" }, [h("h2", { text: floorN(f) }), h("div", { class: "sr-row" }, [
      step(f + 1, t.floor_up, "↑"), step(f - 1, t.floor_down, "↓"),
      h("button", { type: "button", class: "sr-btn", text: t.all_hotel, onclick: () => S.nav && S.nav.collapse() }),
    ])]);
    if (!model.isSale(f)) {
      const label = model.floorLabel(f);
      return [head, h("p", { class: "sr-muted", text: label ? `${label}. ${t.no_sale}` : t.no_plan }), floorButtons()];
    }
    const rooms = model.roomsOf(f);
    const inGroup = rooms.filter((r) => S.rooms(state.audience).includes(r.number)).length;
    const stats = fill(t.floor_rooms, { n: rooms.length, rooms: plural(lang, rooms.length, t.stat_rooms) }) + (inGroup && state.audience === "group" ? ` · ${fill(t.floor_in_group, { n: inGroup })}` : "");
    const cur = state.selRoom && model.rooms.get(state.selRoom);
    const info = cur && cur.floor === f ? `${cur.number} · ${model.typeName(cur.type_key)} · ${model.sideName(cur.side)}` : t.hint_floor_open;
    return [head, h("p", { class: "sr-muted", text: stats }), planSvg(f), h("p", { class: "sr-roominfo", text: info }), floorButtons()];
  }

  let signature = "";
  function render() {
    const sig = JSON.stringify([state.tab, state.audience, state.group, state.short, state.gsize, state.booking, state.selRoom, state.selFloor, state.reqOpen]);
    if (sig === signature) return;
    signature = sig;
    for (const b of tabButtons) b.setAttribute("aria-selected", String(b.dataset.tab === state.tab));
    // Форма запроса живёт своим узлом: перед перерисовкой забираем её, чтобы не потерять ввод.
    const form = document.getElementById("showroom-request");
    const parts = state.tab === "group" ? groupPane() : state.tab === "booking" ? bookingPane() : floorsPane();
    pane.replaceChildren(...parts);
    const slot = pane.querySelector('[data-sr-slot="request"]');
    if (slot && form) slot.appendChild(form);
    else if (form && !form.isConnected) document.body.appendChild(form);
    if (form) form.hidden = !slot;
  }
  return { render };
}
