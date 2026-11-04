/* Общее страницы 3D-витрины (#816, часть Б): разметка без innerHTML, надписи и склонения,
 * номера сцены и подбор группы — по прототипу, но только из чисел сцены (NG-7).
 *
 * Подбор группы — правило прототипа: один номер на человека, как можно меньше этажей подряд,
 * больше номеров «ведущей» стороны (её вид — первый в `views` сцены), выше — лучше; внутри —
 * сначала ведущая сторона. Наличия здесь нет и быть не может (NG-2): только раскладка.
 */

export function h(tag, attrs, children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children || []) if (child) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  return node;
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function svg(tag, attrs, children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "text") node.textContent = value;
    else if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  }
  for (const child of children || []) if (child) node.appendChild(child);
  return node;
}

// Значки прототипа: только пути, без внешних файлов.
const ICON_PATHS = {
  rotate: ["M20 12a8 8 0 1 1-2.34-5.66", "M20 4v5h-5"],
  zoom: ["M10.5 4a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13", "m15.5 15.5 5 5M8 10.5h5M10.5 8v5"],
  pan: ["M12 3v18M3 12h18M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"],
  mountains: ["m2.5 19.5 6.5-11 4 6.5 2.5-3.5 6 8z"],
  city: ["M4 20V10h5v10M9 20V4h6v16M15 20v-7h5v7M2.5 20h19"],
  top: ["M12 3v11M7.5 9.5 12 14l4.5-4.5M4 20h16"],
  home: ["M4 12a8 8 0 1 0 2.34-5.66", "M4 4v5h5"],
  chat: ["M4 5.5h16v10H10l-4.5 3.5v-3.5H4z"],
  dots: ["M8 10.5h.01M12 10.5h.01M16 10.5h.01"],
  building: ["M8 21V5.5L12 3l4 2.5V21M4.5 21h15M10.5 8.5h3M10.5 12h3M10.5 15.5h3"],
  back: ["M14.5 5.5 8 12l6.5 6.5"],
  search: ["M10.5 4a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13", "m15.5 15.5 5 5"],
  send: ["M5 12h13M13 6.5 18.5 12 13 17.5"],
  down: ["m6 9.5 6 6 6-6"],
  ok: ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "m8 12.5 2.7 2.7L16.5 9.5"],
};
export function icon(name, width = 2) {
  return svg("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": width, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    (ICON_PATHS[name] || []).map((d) => svg("path", { d })));
}

export const fill = (template, vars) => String(template || "").replace(/\{(\w+)\}/g, (_, key) => String(vars[key] ?? ""));
const lowerFirst = (s) => (s ? s.charAt(0).toLowerCase() + s.slice(1) : s);

// «один|несколько|много»: ru — три формы, en — две, kk — одна.
export function plural(lang, n, forms) {
  const f = String(forms || "").split("|");
  if (f.length === 1) return f[0];
  if (lang === "ru" && f.length >= 3) {
    const m10 = n % 10;
    const m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return f[0];
    if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return f[1];
    return f[2];
  }
  return n === 1 ? f[0] : f[1];
}

// 5, 7, 8, 9, 13 → «5, 7–9, 13».
export function ranges(list) {
  const out = [];
  const sorted = [...list].sort((a, b) => a - b);
  for (let i = 0; i < sorted.length; i += 1) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j += 1;
    out.push(i === j ? `${sorted[i]}` : `${sorted[i]}–${sorted[j]}`);
    i = j;
  }
  return out.join(", ");
}

export function buildModel(scene, lang) {
  const textFor = (value) => (value ? value[lang] || value.en || "" : "");
  const viewOrder = Object.keys(scene.views);
  const sideKeys = Object.keys(scene.sides);
  // Ведущая сторона — та, откуда виден первый вид сцены; её номера в группе — первыми.
  // Такой стороны нет — нет и фраз о ней (подбор тогда просто по этажам).
  const leadSide = sideKeys.find((key) => (scene.sides[key].view_keys || []).includes(viewOrder[0])) || null;
  const rooms = new Map();
  const floors = new Map();
  for (const floor of scene.floors) {
    floors.set(floor.number, floor);
    for (const room of floor.rooms) {
      const views = room.view_keys && room.view_keys.length ? room.view_keys : (scene.sides[room.side] || {}).view_keys || [];
      rooms.set(room.number, { number: room.number, floor: floor.number, side: room.side, type_key: room.type_key, views, lead: room.side === leadSide });
    }
  }
  const saleFloors = scene.floors.filter((f) => f.rooms.length && f.sellable !== false).map((f) => f.number).sort((a, b) => a - b);
  const sideName = (key) => textFor((scene.sides[key] || {}).names) || key;
  const typeName = (key) => textFor((scene.room_types[key] || {}).names) || key;
  const roomsOf = (n) => (floors.get(n) || { rooms: [] }).rooms.map((r) => rooms.get(r.number));
  // Образец номера для подсказок — первый номер верхнего этажа продаж.
  const top = saleFloors.length ? roomsOf(saleFloors[saleFloors.length - 1]) : [];
  const exampleRoom = top.length ? top[0].number : "";
  const secondType = Object.keys(scene.room_types).find((key) => key !== (top[0] || {}).type_key) || Object.keys(scene.room_types)[0];
  return {
    scene, textFor, rooms, floors, saleFloors, leadSide, exampleRoom, secondType,
    sideName, typeName, roomsOf,
    sideNameLower: (key) => lowerFirst(sideName(key)),
    floorLabel: (n) => textFor((floors.get(n) || {}).label),
    isSale: (n) => saleFloors.includes(n),
  };
}

const cmp = (a, b) => {
  for (let i = 0; i < a.length; i += 1) if (a[i] !== b[i]) return a[i] - b[i];
  return 0;
};

// Подбор группы прототипа: возвращает {rooms: [номера], short: сколько всего, если не хватило}.
export function pickGroup(model, n) {
  const list = model.saleFloors;
  let best = null;
  const consider = (fl) => {
    const rooms = fl.flatMap((f) => model.roomsOf(f));
    if (rooms.length < n) return;
    const lead = rooms.filter((r) => r.lead).length;
    const score = [fl.length, -Math.min(n, lead), -fl[fl.length - 1]];
    if (!best || cmp(score, best.score) < 0) best = { rooms, score };
  };
  for (const strict of [true, false]) {
    for (let len = 1; len <= list.length && !best; len += 1) {
      for (let i = 0; i + len <= list.length; i += 1) {
        const fl = list.slice(i, i + len);
        if (strict && fl[len - 1] - fl[0] !== len - 1) continue;
        consider(fl);
      }
    }
    if (best) break;
  }
  const all = best ? best.rooms : list.flatMap((f) => model.roomsOf(f));
  const sorted = all.slice().sort((a, b) => (a.lead === b.lead ? 0 : a.lead ? -1 : 1) || a.floor - b.floor || a.number.localeCompare(b.number, undefined, { numeric: true }));
  return { rooms: sorted.slice(0, n).map((r) => r.number), short: best ? 0 : all.length };
}

// Сводка группы: «Подобрано 30 номеров · этажи 15–16 · 24 — вид на горы, 6 — вид на город».
export function groupSummary(model, numbers, t, lang, template) {
  const rooms = numbers.map((n) => model.rooms.get(n)).filter(Boolean);
  const fl = [...new Set(rooms.map((r) => r.floor))];
  const floorsText = fill(fl.length === 1 ? t.floors_one : t.floors_many, { list: ranges(fl) });
  const bySide = new Map();
  rooms.forEach((r) => bySide.set(r.side, (bySide.get(r.side) || 0) + 1));
  const sides = [...bySide.entries()]
    .sort((a, b) => (a[0] === model.leadSide ? -1 : b[0] === model.leadSide ? 1 : b[1] - a[1]))
    .map(([side, count]) => `${count} — ${model.sideNameLower(side)}`);
  // Части заголовка — неразрывными кусками, как в прототипе («этажи 15–16» не рвётся):
  // [[куски через « · »], [стороны через «, »]].
  const head = fill(template || t.picked, { n: rooms.length, rooms: plural(lang, rooms.length, t.stat_rooms), floors: floorsText });
  return { head, parts: head.split(" · "), sides, floors: fl.sort((a, b) => a - b), rooms };
}
