"""Страница «Заказать в номер» веб-чата — демо к презентации (issue #438).

Кликабельный макет на выдуманных данных: ни записи в БД, ни вызовов модели,
ни запросов к серверу после загрузки — корзины, оформление и шаги статуса
живут в памяти страницы. Отдаёт её маршрут `GET /g/{slug}/{room}/orders`
(`router.py`) только за живой гостевой сессией и при `ENABLE_DEMO_ORDERS`.

Устроена как страница чата (`page.py`): один HTML с инлайн CSS/JS, без
сборки. Каталог (`channels/common/demo_orders.py`) и надписи
(`demo_orders_texts.py`) вшиваются JSON'ом через маркер `__DEMO_DATA__`, а не
`str.format` — в HTML полно фигурных скобок. Язык — kk/ru/en по браузеру,
переключатель в шапке. Удаляется PR'ом настоящего room service (spec 0038).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from hospitality.channels.common.demo_orders import DEMO_CATALOG
from hospitality.channels.web.demo_orders_texts import DEMO_TEXTS

_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Room service · Demo</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --line:#e3e5e8; --accent:#1a73e8; --muted:#667;
          --text:#1c1e21; --ok:#188038; --demo:#fff3cd; --demo-ink:#6b4e00; }
  * { box-sizing:border-box; margin:0; }
  body { font:16px/1.4 system-ui,sans-serif; background:var(--bg); color:var(--text);
         padding-bottom:110px; }
  button { font:inherit; color:inherit; border:0; background:none; cursor:pointer; }
  .strip { position:sticky; top:0; z-index:20; padding:8px 16px; background:var(--demo);
           color:var(--demo-ink); font-size:13px; }
  .strip b { text-transform:uppercase; letter-spacing:.06em; }
  header { display:flex; align-items:center; gap:10px; padding:10px 16px;
           background:var(--card); border-bottom:1px solid var(--line); }
  .back { width:40px; height:40px; flex:none; border-radius:50%; background:var(--bg);
          font-size:24px; line-height:1; }
  .ttl { flex:1; min-width:0; }
  .ttl b { display:block; font-size:17px; }
  .ttl span { font-size:13px; color:var(--muted); }
  .langs { display:flex; flex:none; gap:2px; padding:2px; border-radius:999px;
           background:var(--bg); }
  .langs button { padding:6px 8px; border-radius:999px; font-size:12px; font-weight:600;
                  color:var(--muted); }
  .langs button[aria-pressed="true"] { background:var(--card); color:var(--text);
                                       box-shadow:0 1px 2px rgba(0,0,0,.15); }
  .wrap { max-width:560px; margin:0 auto; padding:12px 16px; }
  .tabs { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .tab { padding:12px; border-radius:12px; background:var(--card); border:1.5px solid var(--line);
         font-weight:600; }
  .tab[aria-selected="true"] { border-color:var(--accent); color:var(--accent); }
  .lead { font-size:13px; color:var(--muted); margin:10px 2px 0; }
  h1.h1 { font-size:20px; margin:4px 2px 12px; }
  h2 { font-size:13px; margin:18px 2px 8px; color:var(--muted); text-transform:uppercase;
       letter-spacing:.05em; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px; }
  .row { display:flex; align-items:center; gap:12px; padding:10px 12px;
         border-top:1px solid var(--line); }
  .row:first-child { border-top:0; }
  .emoji { width:44px; height:44px; flex:none; display:grid; place-items:center; font-size:24px;
           border-radius:12px; background:var(--bg); }
  .name { flex:1; min-width:0; }
  .name b { display:block; font-weight:500; overflow-wrap:anywhere; }
  .name span { font-size:14px; color:var(--muted); font-variant-numeric:tabular-nums; }
  .plus { width:40px; height:40px; flex:none; border-radius:50%; background:var(--accent);
          color:#fff; font-size:22px; }
  .step { display:flex; flex:none; align-items:center; border-radius:999px; background:var(--bg); }
  .step button { width:38px; height:38px; font-size:20px; }
  .step b { min-width:18px; text-align:center; }
  .pill { display:flex; width:100%; justify-content:space-between; gap:8px; margin-top:12px;
          padding:12px 14px; border-radius:12px; background:var(--card);
          border:1.5px solid var(--ok); font-weight:600; }
  .pill span { color:var(--ok); }
  .chips { display:flex; gap:8px; margin-top:8px; }
  .chip { padding:8px 14px; border-radius:999px; border:1.5px solid var(--line);
          background:var(--card); font-weight:600; }
  .chip[aria-pressed="true"] { border-color:var(--accent); background:var(--accent); color:#fff; }
  .label { font-size:13px; font-weight:600; color:var(--muted); margin:16px 2px 6px; }
  textarea { width:100%; min-height:64px; padding:10px 12px; border:1px solid #c9ccd1;
             border-radius:10px; font:inherit; }
  .sum { display:flex; justify-content:space-between; padding:12px; font-size:18px;
         font-weight:700; border-top:1px solid var(--line); }
  .bar { position:fixed; left:0; right:0; bottom:0; padding:8px 16px 14px;
         background:var(--bg); box-shadow:0 -1px 0 var(--line); }
  .bar-note { max-width:528px; margin:0 auto 6px; font-size:13px; font-weight:600;
              color:var(--muted); }
  .cta { display:flex; justify-content:space-between; align-items:center; gap:12px; width:100%;
         max-width:528px; margin:0 auto; min-height:52px; padding:0 18px; border-radius:14px;
         background:var(--accent); color:#fff; font-weight:700; }
  .cta.center { justify-content:center; }
  .status { text-align:center; padding:16px 8px 12px; }
  .status .big { font-size:46px; }
  .status h1 { font-size:21px; margin-top:6px; }
  .status p { color:var(--muted); margin-top:6px; }
  .steps { list-style:none; padding:6px 12px; }
  .steps li { display:flex; align-items:center; gap:12px; padding:8px 0; color:var(--muted); }
  .steps .dot { width:26px; height:26px; flex:none; display:grid; place-items:center;
                border-radius:50%; background:var(--bg); font-size:13px; }
  .steps li.done { color:var(--text); font-weight:600; }
  .steps li.done .dot { background:var(--ok); color:#fff; }
  .steps time { margin-left:auto; font-size:13px; font-weight:400; color:var(--muted); }
  .actions { display:grid; gap:8px; margin-top:14px; }
  .ghost { min-height:48px; border-radius:12px; background:var(--card);
           border:1.5px solid var(--line); font-weight:600; }
  .ghost.demo { border-style:dashed; border-color:var(--demo-ink); background:var(--demo);
                color:var(--demo-ink); }
</style>
</head>
<body>
<div class="strip"><b data-t="demo"></b> · <span data-t="demo_note"></span></div>
<header>
  <button class="back" id="back" type="button">‹</button>
  <div class="ttl"><b data-t="title"></b><span id="room"></span></div>
  <div class="langs">
    <button type="button" data-lang="kk">ҚАЗ</button>
    <button type="button" data-lang="ru">РУС</button>
    <button type="button" data-lang="en">ENG</button>
  </div>
</header>
<main class="wrap" id="view"></main>
<div class="bar" id="bar" hidden></div>
<script id="demo-data" type="application/json">__DEMO_DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("demo-data").textContent);
const chatUrl = location.pathname.replace(/\\/orders\\/?$/, "");
const view = document.getElementById("view");
const bar = document.getElementById("bar");
const SECTIONS = ["restaurant", "shop"];
const EMOJI = {restaurant: "🍽", shop: "🛍"};
const STEPS = ["accepted", "paid", "delivering"];
const items = {};
for (const c of DATA.catalog) for (const i of c.items) items[i.key] = i;

function pickLang() {
  for (const l of navigator.languages || [navigator.language || ""]) {
    const code = String(l).slice(0, 2).toLowerCase();
    if (code === "kk" || code === "ru" || code === "en") return code;
  }
  return "en";
}
// Всё демо — в памяти страницы: обновление возвращает её в начало.
const state = {lang: pickLang(), tab: "restaurant", screen: "menu",
  carts: {restaurant: {}, shop: {}}, pay: {restaurant: "cash", shop: "cash"},
  notes: {restaurant: "", shop: ""}, orders: {restaurant: null, shop: null},
  next: {restaurant: 13, shop: 8}};

function t(key, vars) {
  let s = DATA.texts[key][state.lang];
  for (const [k, v] of Object.entries(vars || {})) s = s.replace("{" + k + "}", v);
  return s;
}
const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ESC[c]); }
function money(n) {
  return n.toLocaleString(state.lang === "en" ? "en-US" : "ru-RU") + "\\u00a0₸";
}
function clock() { return new Date().toTimeString().slice(0, 5); }
function lines(cart) {
  return Object.entries(cart).filter(([, q]) => q > 0).map(([k, q]) => ({item: items[k], qty: q}));
}
function sum(ls) { return ls.reduce((s, l) => s + l.item.price * l.qty, 0); }
function count(ls) { return ls.reduce((s, l) => s + l.qty, 0); }

function stepper(key, qty) {
  const add = `<button data-add="${key}" aria-label="${esc(t("add"))}"`;
  if (!qty) return add.replace("<button", '<button class="plus"') + ">+</button>";
  return `<div class="step"><button data-dec="${key}" aria-label="${esc(t("remove"))}">−</button>` +
    `<b>${qty}</b>${add}>+</button></div>`;
}
function itemRow(i, qty, price) {
  return `<div class="row"><span class="emoji">${i.emoji}</span><div class="name">` +
    `<b>${esc(i.name[state.lang])}</b><span>${price}</span></div>${stepper(i.key, qty)}</div>`;
}
function totalRow(amount) {
  return `<div class="sum"><span>${esc(t("total"))}</span><span>${money(amount)}</span></div>`;
}

function renderMenu() {
  const s = state.tab, cart = state.carts[s], order = state.orders[s];
  let html = '<div class="tabs" role="tablist">' + SECTIONS.map((x) =>
    `<button class="tab" role="tab" data-tab="${x}" aria-selected="${x === s}">` +
    `${EMOJI[x]} ${esc(t("tab_" + x))}</button>`).join("") + "</div>";
  html += `<p class="lead">${esc(t("lead_" + s))}. ${esc(t("pay_note"))}.</p>`;
  if (order) {
    html += `<button class="pill" data-go="order"><b>${esc(t("order_n", {n: order.number}))}</b>` +
      `<span>${esc(t("step_" + STEPS[order.step]))} ›</span></button>`;
  }
  for (const c of DATA.catalog.filter((c) => c.section === s)) {
    html += `<h2>${esc(c.title[state.lang])}</h2><div class="card">` +
      c.items.map((i) => itemRow(i, cart[i.key] || 0, money(i.price))).join("") + "</div>";
  }
  view.innerHTML = html;
  const ls = lines(cart);
  bar.hidden = !ls.length;
  bar.innerHTML = ls.length ? `<p class="bar-note">${esc(t("cart_" + s))}</p>` +
    `<button class="cta" data-go="checkout"><span>🛒 ${count(ls)} · ${money(sum(ls))}</span>` +
    `<span>${esc(t("checkout"))} ›</span></button>` : "";
}

function renderCheckout() {
  const s = state.tab, ls = lines(state.carts[s]);
  if (!ls.length) { state.screen = "menu"; renderMenu(); return; }
  const payChips = ["cash", "card"].map((m) => `<button class="chip" data-pay="${m}" ` +
    `aria-pressed="${m === state.pay[s]}">${esc(t(m))}</button>`).join("");
  const info = (emoji, title, sub) => `<div class="row"><span class="emoji">${emoji}</span>` +
    `<div class="name"><b>${esc(title)}</b>${sub}</div></div>`;
  view.innerHTML =
    `<h1 class="h1">${EMOJI[s]} ${esc(t("your_order"))} · ${esc(t("tab_" + s))}</h1>` +
    '<div class="card">' +
    info("🚪", t("deliver_to", {room: DATA.room}), `<span>${esc(t("by_code"))}</span>`) +
    info("⏱️", t("asap"), "") +
    info("💳", t("payment"), `<div class="chips">${payChips}</div>`) + "</div>" +
    `<p class="label">${esc(t("items"))}</p><div class="card">` +
    ls.map((l) => itemRow(l.item, l.qty, money(l.item.price * l.qty))).join("") +
    totalRow(sum(ls)) + "</div>" +
    `<p class="label">${esc(t("notes"))}</p><textarea id="note" maxlength="200" ` +
    `placeholder="${esc(t("optional"))}">${esc(state.notes[s])}</textarea>`;
  bar.hidden = false;
  bar.innerHTML = '<button class="cta center" data-go="place">' +
    `${esc(t("place_order", {total: money(sum(ls))}))}</button>`;
}

function renderOrder() {
  const s = state.tab, o = state.orders[s];
  if (!o) { state.screen = "menu"; renderMenu(); return; }
  const texts = [t("text_accepted", {total: money(o.total), method: t("method_" + o.pay)}),
    t("text_paid_" + s, {room: DATA.room}), t("text_delivering", {room: DATA.room})];
  const icons = ["✅", s === "restaurant" ? "👨‍🍳" : "📦", "🛎️"];
  const steps = STEPS.map((k, i) => `<li class="${i <= o.step ? "done" : ""}">` +
    `<span class="dot">${i <= o.step ? "✓" : i + 1}</span>${esc(t("step_" + k))}` +
    `<time>${o.times[i] || ""}</time></li>`).join("");
  const rows = o.lines.map((l) => `<div class="row"><span class="emoji">${l.item.emoji}</span>` +
    `<div class="name"><b>${esc(l.item.name[state.lang])} ×${l.qty}</b></div>` +
    `<span>${money(l.item.price * l.qty)}</span></div>`).join("");
  const next = o.step < STEPS.length - 1
    ? `<button class="ghost demo" data-go="next">${esc(t("demo_next"))} ›</button>` : "";
  view.innerHTML = `<div class="status"><div class="big">${icons[o.step]}</div>` +
    `<h1>${esc(t("order_n", {n: o.number}))} · ${esc(t("step_" + STEPS[o.step]))}</h1>` +
    `<p>${esc(texts[o.step])}</p></div><div class="card"><ol class="steps">${steps}</ol></div>` +
    `<p class="label">${esc(t("items"))}</p><div class="card">${rows}${totalRow(o.total)}</div>` +
    `<div class="actions">${next}<button class="ghost" data-go="more">${esc(t("order_more"))}` +
    `</button><button class="ghost" data-go="chat">${esc(t("to_chat"))}</button></div>`;
  bar.hidden = true;
}

function render() {
  document.documentElement.lang = state.lang;
  for (const el of document.querySelectorAll("[data-t]")) el.textContent = t(el.dataset.t);
  for (const b of document.querySelectorAll("[data-lang]")) {
    b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang));
  }
  document.getElementById("room").textContent = "№ " + DATA.room;
  document.getElementById("back").setAttribute("aria-label", t("back"));
  if (state.screen === "checkout") renderCheckout();
  else if (state.screen === "order") renderOrder();
  else renderMenu();
}

function placeOrder(s) {
  const ls = lines(state.carts[s]);
  state.orders[s] = {number: state.next[s]++, lines: ls, total: sum(ls), pay: state.pay[s],
    step: 0, times: [clock()]};
  state.carts[s] = {}; state.notes[s] = "";
}

document.addEventListener("input", (e) => {
  if (e.target.id === "note") state.notes[state.tab] = e.target.value;
});
document.addEventListener("click", (e) => {
  const el = e.target.closest("button");
  if (!el) return;
  const s = state.tab, cart = state.carts[s], d = el.dataset;
  let top = true;
  if (d.lang) { state.lang = d.lang; top = false; }
  else if (d.tab) { state.tab = d.tab; state.screen = "menu"; }
  else if (d.add) { cart[d.add] = (cart[d.add] || 0) + 1; top = false; }
  else if (d.dec) { cart[d.dec] = Math.max(0, (cart[d.dec] || 0) - 1); top = false; }
  else if (d.pay) { state.pay[s] = d.pay; top = false; }
  else if (el.id === "back" && state.screen === "menu") { location.href = chatUrl; return; }
  else if (el.id === "back") { state.screen = "menu"; }
  else if (d.go === "checkout" || d.go === "order") { state.screen = d.go; }
  else if (d.go === "place") { placeOrder(s); state.screen = "order"; }
  else if (d.go === "next") { const o = state.orders[s]; o.step += 1; o.times[o.step] = clock(); }
  else if (d.go === "more") { state.orders[s] = null; state.screen = "menu"; }
  else if (d.go === "chat") { location.href = chatUrl; return; }
  else return;
  render();
  if (top) window.scrollTo(0, 0);
});
render();
</script>
</body>
</html>
"""


def _payload(room_number: str) -> str:
    """Данные страницы одной JSON-строкой: комната из сессии, надписи, каталог.

    `<` экранируется: JSON живёт внутри `<script>`, и `</script>` в данных
    (комнату вносит персонал) закрыл бы его раньше времени.
    """
    data: dict[str, Any] = {
        "room": room_number,
        "texts": {key: asdict(text) for key, text in DEMO_TEXTS.items()},
        "catalog": [
            {
                "section": category.section.value,
                "title": asdict(category.title),
                "items": [
                    {
                        "key": item.key,
                        "emoji": item.emoji,
                        "price": item.price_kzt,
                        "name": asdict(item.name),
                    }
                    for item in category.items
                ],
            }
            for category in DEMO_CATALOG
        ],
    }
    return json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")


def render(room_number: str) -> str:
    """HTML демо-страницы для комнаты гостевой сессии."""
    return _PAGE.replace("__DEMO_DATA__", _payload(room_number))
