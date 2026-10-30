// «Заказать в номер» (spec 0038 §4–§5). Данные — JSON `…/menu/data`, итог
// считает сервер (`…/menu/quote`, Р-5), заказ — `POST …/orders`. Всё, что пришло
// с сервера, выводится через esc(): названия и описания вносит персонал (LATER 15.10).
"use strict";
(() => {
  const T = JSON.parse(document.getElementById("menu-texts").textContent);
  const $ = (s, r) => (r || document).querySelector(s);
  const base = location.pathname.replace(/\/menu\/?$/, "");
  const app = $("#app"), bar = $("#bar"), barInner = $("#bar-inner");
  const sheet = $("#sheet"), scrim = $("#scrim"), toastEl = $("#toast");
  const LANG_LABELS = {kk: "ҚАЗ", ru: "РУС", en: "ENG"};
  const ACTIVE = ["sent", "coming", "paid"];
  const PLATES = [["#f6d7a7", "#e9a96b"], ["#fbe7a1", "#f0c25a"], ["#f2a2a2", "#c2414b"], ["#cfe9b4", "#7fb35a"],
    ["#f7c59a", "#d9773d"], ["#e1cbb6", "#7d5233"], ["#c9dcf5", "#6f98d6"], ["#e3cff3", "#9a6cc8"]];
  const state = {lang: "en", data: null, catalog: null, screen: "loading", carts: {}, comments: {},
    quote: null, key: null, sending: false, order: null, poll: null, sheetItem: null, sheetQty: 1};

  // ---------- утилиты ----------
  const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);
  function t(key, vars) {
    const entry = T[key];
    let s = entry ? (entry[state.lang] || entry.en) : key;
    for (const [k, v] of Object.entries(vars || {})) s = s.split("{" + k + "}").join(String(v));
    return s;
  }
  const tc = (key, vars) => t(key + "_" + state.catalog, vars);
  const L = (text) => (text ? text[state.lang] || text.en : "");
  const money = (minor) => String(Math.floor(minor / 100)).replace(/\B(?=(\d{3})+(?!\d))/g, state.lang === "en" ? "," : " ") + " ₸";
  const pct = (p) => String(Number(p));
  const clock = (iso) => (iso ? new Date(iso).toTimeString().slice(0, 5) : "");
  const svg = (d, w) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' + (w || 2) +
    '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + "</svg>";
  const I = {
    back: svg('<path d="M15 5l-7 7 7 7"/>', 2.2), plus: svg('<path d="M12 5v14M5 12h14"/>', 2.4),
    minus: svg('<path d="M5 12h14"/>', 2.4), chev: svg('<path d="M9 5l7 7-7 7"/>', 2.2),
    check: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>', 2.6), x: svg('<path d="M6 6l12 12M18 6L6 18"/>', 2.4),
    door: svg('<path d="M5 21V4a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v17M3 21h18"/><circle cx="15" cy="12.5" r="1" fill="currentColor"/>'),
    wallet: svg('<rect x="3" y="6" width="18" height="13" rx="2.5"/><path d="M3 10h18M16 14.5h2"/>'),
    clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    chef: svg('<path d="M7 14v6h10v-6M7 14a4 4 0 1 1 1.5-7.7A4 4 0 0 1 15.5 6 4 4 0 1 1 17 14z"/><path d="M7 17h10"/>'),
    bag: svg('<path d="M5 8h14l-1 12H6zM9 8V6a3 3 0 0 1 6 0v2"/>'),
    info: svg('<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>'),
    chat: svg('<path d="M4 5h16v11H9l-5 4z"/>'),
  };
  const store = {
    get(k) { try { return JSON.parse(sessionStorage.getItem(k) || "null"); } catch (e) { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* без хранилища — в памяти */ } },
  };
  function toast(text) {
    toastEl.textContent = text; toastEl.classList.add("show");
    clearTimeout(toast.timer); toast.timer = setTimeout(() => toastEl.classList.remove("show"), 3200);
  }
  async function api(path, options) {
    let response;
    try {
      response = await fetch(base + path, Object.assign({headers: {"Content-Type": "application/json"}}, options));
    } catch (e) { return {ok: false, status: 0, code: null, data: null}; }
    let data = null;
    try { data = await response.json(); } catch (e) { /* не JSON */ }
    if (response.status === 401) { location.href = base; }
    const code = data && data.error ? data.error.code : null;
    return {ok: response.ok, status: response.status, code, data};
  }
  const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9_-]/g, "");

  // ---------- данные ----------
  const cat = () => state.data.catalogs.find((c) => c.catalog === state.catalog);
  const itemsOf = (c) => Object.fromEntries(c.sections.flatMap((s) => s.items).map((i) => [i.id, i]));
  const cart = () => state.carts[state.catalog] || (state.carts[state.catalog] = {});
  function cartLines() {
    const items = itemsOf(cat());
    return Object.entries(cart()).filter(([id, q]) => q > 0 && items[id]).map(([id, qty]) => ({id, qty, item: items[id]}));
  }
  const count = (lines) => lines.reduce((a, l) => a + l.qty, 0);
  function estimate(lines) {
    const subtotal = lines.reduce((a, l) => a + l.item.price_minor * l.qty, 0);
    const charge = Math.round((subtotal / 100) * Number(cat().service_charge_percent) / 100) * 100;
    return {subtotal_minor: subtotal, service_charge_minor: charge, total_minor: subtotal + charge,
      service_charge_percent: cat().service_charge_percent};
  }
  function save() { store.set("rs-carts", state.carts); store.set("rs-comments", state.comments); }
  async function loadData() {
    const r = await api("/menu/data", {method: "GET"});
    if (!r.ok) { if (r.status === 404) location.href = base; return false; }
    state.data = r.data;
    const langs = state.data.languages;
    if (!langs.includes(state.lang)) state.lang = langs.includes("en") ? "en" : langs[0];
    if (!state.data.catalogs.some((c) => c.catalog === state.catalog)) state.catalog = state.data.catalogs.length ? state.data.catalogs[0].catalog : null;
    return true;
  }

  // ---------- кусочки разметки ----------
  // Плашка с буквой — всегда; фото (PR 7) ложится поверх. Не загрузилось — уходит, плашка остаётся.
  function plate(item) {
    let h = 0;
    for (const ch of String(item.id)) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    const [a, b] = PLATES[h % PLATES.length];
    const img = item.photo_file_id ? '<img src="' + esc(base + "/menu/photos/" + item.photo_file_id) + '" alt="" loading="lazy" decoding="async">' : "";
    return '<span class="photo" style="--a:' + a + ";--b:" + b + '" aria-hidden="true">' + esc(L(item.names).charAt(0).toUpperCase()) + img + "</span>";
  }
  function topbar(act, label, title, sub, extra) {
    return '<header class="topbar"><button class="icon-btn" data-act="' + act + '" aria-label="' + esc(label) + '">' + I.back +
      '</button><div class="topbar-title"><b>' + esc(title) + "</b>" + (sub ? "<span>" + esc(sub) + "</span>" : "") + "</div>" + (extra || "") + "</header>";
  }
  function langSwitch() {
    if (state.data.languages.length < 2) return "";
    return '<div class="langs" role="group" aria-label="Language">' + state.data.languages.map((l) =>
      '<button data-lang="' + esc(l) + '" aria-pressed="' + (l === state.lang) + '">' + esc(LANG_LABELS[l] || l) + "</button>").join("") + "</div>";
  }
  function control(item) {
    const q = cart()[item.id] || 0;
    if (!item.is_available) return '<span class="sold-out">' + esc(t("sold_out")) + "</span>";
    if (!cat().accepting_orders) return "";
    if (!q) return '<button class="add-btn" data-add="' + esc(item.id) + '" aria-label="' + esc(t("add") + ": " + L(item.names)) + '">' + I.plus + "</button>";
    return '<div class="stepper pop"><button data-dec="' + esc(item.id) + '" aria-label="' + esc(t("less")) + '">' + I.minus + '</button><b class="num">' + q +
      '</b><button data-add="' + esc(item.id) + '" aria-label="' + esc(t("more")) + '">' + I.plus + "</button></div>";
  }
  function card(item) {
    const desc = L(item.descriptions);
    return '<article class="dish' + (item.is_available ? "" : " stopped") + '"><div class="dish-photo-wrap"><button class="dish-open" data-open="' +
      esc(item.id) + '" aria-label="' + esc(L(item.names)) + '">' + plate(item) + "</button>" + control(item) + "</div>" +
      '<div class="dish-body"><span class="dish-price num">' + money(item.price_minor) + '</span><h3 class="dish-name">' + esc(L(item.names)) +
      "</h3>" + (desc ? '<p class="dish-desc">' + esc(desc) + "</p>" : "") + "</div></article>";
  }
  function sums(q) {
    return '<div class="sum-row"><span>' + esc(tc("subtotal")) + '</span><span class="num">' + money(q.subtotal_minor) + "</span></div>" +
      (Number(q.service_charge_percent) > 0 ? '<div class="sum-row"><span>' + esc(t("charge", {percent: pct(q.service_charge_percent)})) +
        '</span><span class="num">' + money(q.service_charge_minor) + "</span></div>" : "") +
      '<div class="sum-row total"><span>' + esc(t("total")) + '</span><span class="num">' + money(q.total_minor) + "</span></div>";
  }
  function setBar(html) { bar.hidden = !html; barInner.innerHTML = html || ""; }

  // ---------- экраны ----------
  function render() {
    document.documentElement.lang = state.lang;
    if (state.screen === "loading") { app.innerHTML = '<div class="loading"><span class="spin"></span></div>'; setBar(""); return; }
    ({menu: renderMenu, cart: renderCart, checkout: renderCheckout, order: renderOrder})[state.screen]();
  }
  function go(screen) { state.screen = screen; closeSheet(); render(); window.scrollTo(0, 0); }

  function renderMenu() {
    const c = cat();
    let h = topbar("chat", t("back_chat"), t("title"), t("room", {room: state.data.room_number}), langSwitch());
    if (!c) { app.innerHTML = h + '<p class="empty">' + esc(t("empty_catalog")) + "</p>"; setBar(""); return; }
    if (state.data.catalogs.length > 1) {
      h += '<div class="tabs" role="tablist">' + state.data.catalogs.map((x) => '<button role="tab" data-tab="' + esc(x.catalog) +
        '" aria-selected="' + (x.catalog === state.catalog) + '">' + (x.catalog === "shop" ? "🛍 " : "🍽 ") + esc(t("tab_" + x.catalog)) + "</button>").join("") + "</div>";
    }
    h += '<div class="hero"><h1>' + esc(tc("hero")) + '</h1><div class="facts"><span class="fact">' + I.wallet + esc(t("fact_pay")) +
      '</span><span class="fact">' + (c.delivery_note ? I.clock + esc(L(c.delivery_note)) : I.chef + esc(tc("fact_after"))) + "</span></div></div>";
    if (!c.accepting_orders) h += '<div class="notice" role="status">' + I.info + "<b>" + esc(tc("closed")) + "</b><span>" + esc(t("closed_note")) + "</span></div>";
    for (const o of state.data.orders.filter((x) => x.catalog === state.catalog)) {
      h += '<button class="order-pill" data-order="' + esc(o.id) + '"><span class="dot' + (o.stage === "paid" ? " paid" : "") + '"></span><div><b>' +
        esc(t("order_n", {n: o.number}) + " · " + t("stage_" + o.stage)) + "</b><span>" + esc(stepSub(o, o.stage)) + "</span></div>" + I.chev + "</button>";
    }
    if (!c.sections.length) { app.innerHTML = h + '<p class="empty">' + esc(t("empty_catalog")) + "</p>"; setBar(""); return; }
    h += '<nav class="chips-wrap"><div class="chips" id="chips">' + c.sections.map((s, n) => '<button class="chip" data-chip="' + n + '" aria-current="' + (n === 0) + '">' + esc(L(s.names)) + "</button>").join("") + "</div></nav>";
    h += c.sections.map((s, n) => '<section class="section" id="sec-' + n + '" data-section="' + n + '"><h2>' + esc(L(s.names)) + '</h2><div class="grid">' + s.items.map(card).join("") + "</div></section>").join("");
    app.innerHTML = h;
    const lines = cartLines();
    if (!c.accepting_orders) setBar('<button class="cta center" disabled>' + esc(t("closed_bar")) + "</button>");
    else if (lines.length) setBar('<button class="cta rise" data-act="cart"><span class="left"><span class="count num">' + count(lines) + "</span>" + esc(t("cart")) + '</span><span class="num">' + money(estimate(lines).subtotal_minor) + "</span></button>");
    else setBar("");
    spy();
  }

  function lineRows(lines, editable) {
    return lines.map((l) => {
      const bad = editable && !l.item.is_available;
      const sub = bad ? '<span class="line-sub bad">' + esc(t("sold_out_line")) + "</span>" : '<span class="line-sub num">' + money(l.unit) + (editable ? "" : " × " + l.qty) + "</span>";
      const right = editable ? '<div class="mini-stepper"><button data-dec="' + esc(l.id) + '" aria-label="' + esc(t("less")) + '">' + I.minus + '</button><b class="num">' + l.qty +
        '</b><button data-add="' + esc(l.id) + '" aria-label="' + esc(t("more")) + '"' + (bad ? " disabled" : "") + ">" + I.plus + "</button></div>" : '<span class="num">' + money(l.unit * l.qty) + "</span>";
      return '<div class="line"><div><div class="line-name">' + esc(L(l.names)) + "</div>" + sub + "</div>" + right + "</div>";
    }).join("");
  }
  const fromCart = (lines) => lines.map((l) => ({id: l.id, qty: l.qty, unit: l.item.price_minor, names: l.item.names, item: l.item}));
  const fromOrder = (lines) => lines.map((l) => ({id: l.item_id, qty: l.quantity, unit: l.unit_price_minor, names: l.names}));

  function renderCart() {
    const lines = cartLines();
    if (!lines.length) { go("menu"); return; }
    const q = estimate(lines), bad = lines.some((l) => !l.item.is_available), open = cat().accepting_orders;
    app.innerHTML = topbar("menu", t("back"), t("cart"), "", '<button class="text-btn danger" data-act="clear">' + esc(t("clear")) + "</button>") +
      '<div class="card">' + lineRows(fromCart(lines), true) + '</div><div class="card card-pad">' + sums(q) + "</div>" +
      (Number(q.service_charge_percent) > 0 ? '<p class="hint">' + esc(tc("charge_hint", {percent: pct(q.service_charge_percent)})) + "</p>" : "");
    setBar('<button class="cta" data-act="next"' + (bad || !open || state.sending ? " disabled" : "") + "><span>" + esc(t("next")) + '</span><span class="num">' + money(q.total_minor) + "</span></button>" +
      (bad ? '<div class="bar-note">' + esc(tc("remove_sold_out")) + "</div>" : !open ? '<div class="bar-note">' + esc(t("err_closed")) + "</div>" : ""));
  }

  function renderCheckout() {
    const q = state.quote;
    if (!q) { go("cart"); return; }
    const note = cat().delivery_note ? L(cat().delivery_note) : tc("fact_after");
    app.innerHTML = topbar("cart", t("back"), t("checkout"), t("checkout_sub")) +
      '<div class="card"><div class="info-row"><span class="info-ico">' + I.door + "</span><div><b>" + esc(t("deliver_room", {room: state.data.room_number})) + "</b><span>" + esc(t("by_code")) + "</span></div></div>" +
      '<div class="info-row"><span class="info-ico">' + I.clock + "</span><div><b>" + esc(t("asap")) + "</b><span>" + esc(note) + "</span></div></div>" +
      '<div class="info-row"><span class="info-ico">' + I.wallet + "</span><div><b>" + esc(t("pay_title")) + "</b><span>" + esc(t("pay_sub")) + "</span></div></div></div>" +
      '<div class="label">' + esc(t("items")) + '</div><div class="card">' + lineRows(fromOrder(q.lines), false) + "</div>" +
      '<div class="label"><label for="comment">' + esc(t("comment")) + '</label></div><textarea id="comment" maxlength="200" placeholder="' + esc(tc("comment_placeholder")) + '">' +
      esc(state.comments[state.catalog] || "") + '</textarea><p class="hint">' + esc(t("comment_hint")) + '</p><div class="card card-pad" style="margin-top:14px">' + sums(q) + "</div>";
    setBar('<button class="cta" data-act="place"' + (state.sending ? " disabled" : "") + ">" + (state.sending
      ? '<span class="left"><span class="spin"></span>' + esc(t("sending")) + "</span><span></span>"
      : "<span>" + esc(t("place")) + '</span><span class="num">' + money(q.total_minor) + "</span>") + "</button>");
  }

  function stepSub(o, stage) {
    return {sent: t("step_sent_" + o.catalog), coming: t("step_coming"), paid: t("step_paid_" + o.catalog), delivered: t("step_delivered")}[stage] || "";
  }
  function reasonKey(o) {
    const r = o.cancel_reason && o.cancel_reason !== "by_guest" ? o.cancel_reason : "other";
    return T["reason_" + r] ? "reason_" + r : "reason_" + r + "_" + o.catalog;
  }
  function renderOrder() {
    const o = state.order;
    if (!o) { go("menu"); return; }
    state.catalog = o.catalog;
    const s = o.stage;
    const title = s === "coming" || s === "delivered" || s === "cancelled" ? t("title_" + s) : tc("title_" + s);
    let text = {sent: tc("text_sent"), coming: t("text_coming", {total: money(o.total_minor)}), paid: tc("text_paid", {room: o.room_number || ""}), delivered: tc("text_delivered")}[s];
    if (s === "cancelled") {
      text = o.cancel_reason === "by_guest" ? t("text_cancelled_guest")
        : t("text_cancelled_staff", {reason: t(reasonKey(o))}) + (o.paid_at ? " " + t("text_refund") : "");
    }
    const times = {sent: o.created_at, coming: o.coming_at, paid: o.paid_at, delivered: s === "delivered" ? o.closed_at : null};
    const order = ["sent", "coming", "paid", "delivered"];
    const reached = order.indexOf(s);
    let steps = order.map((k, i) => {
      const done = !!times[k] || (s !== "cancelled" && i <= reached);
      const now = s !== "cancelled" && !done && i === reached + 1;
      return '<li class="step ' + (done ? "done" : now ? "now" : "todo") + '"><span class="step-dot">' + (done ? I.check : "") + "</span><div><b>" + esc(t("stage_" + k)) +
        "</b><span>" + esc(stepSub(o, k)) + "</span></div><time>" + esc(clock(times[k])) + "</time></li>";
    }).join("");
    if (s === "cancelled") steps += '<li class="step cancel"><span class="step-dot">' + I.x + "</span><div><b>" + esc(t("stage_cancelled")) + "</b></div><time>" + esc(clock(o.closed_at)) + "</time></li>";
    const ico = s === "cancelled" ? ["cancel", I.x] : s === "delivered" ? ["", I.check] : s === "paid" ? ["", I.chef] : ["wait", I.clock];
    app.innerHTML = topbar("menu", t("back"), t("order_n", {n: o.number}), t("room", {room: o.room_number || ""})) +
      '<div class="status-head"><span class="status-ico ' + ico[0] + '">' + ico[1] + "</span><h1>" + esc(title) + "</h1><p>" + esc(text) + "</p></div>" +
      '<div class="card"><ol class="steps">' + steps + "</ol></div>" +
      '<div class="label">' + esc(t("items")) + '</div><div class="card">' + lineRows(fromOrder(o.lines), false) + "</div>" +
      (o.comment ? '<div class="card card-pad"><span class="line-sub">' + esc(t("comment")) + "</span><div>" + esc(o.comment) + "</div></div>" : "") +
      '<div class="card card-pad">' + sums(o) + '</div><div class="actions">' +
      /* Заказ принят только после оплаты: до неё гость отменяет сам. */
      (s === "sent" || s === "coming" ? '<button class="ghost-btn danger" data-act="ask-cancel">' + esc(t("cancel")) + "</button>" : "") +
      '<a class="ghost-btn" href="' + esc(base) + '">' + I.chat + esc(t("write_chat")) + "</a></div>";
    setBar(ACTIVE.includes(s) ? "" : '<button class="cta center" data-act="more">' + esc(t("order_more")) + "</button>");
  }

  // ---------- листы ----------
  function openSheet(html) {
    sheet.innerHTML = '<div class="grabber" aria-hidden="true"></div>' + html;
    sheet.hidden = false; scrim.hidden = false;
    requestAnimationFrame(() => { sheet.classList.add("open"); scrim.classList.add("open"); });
  }
  function closeSheet() {
    if (sheet.hidden) return;
    sheet.classList.remove("open"); scrim.classList.remove("open"); state.sheetItem = null;
    setTimeout(() => { if (!state.sheetItem) { sheet.hidden = true; scrim.hidden = true; } }, 260);
  }
  function openItem(id) {
    const item = itemsOf(cat())[id];
    if (!item) return;
    state.sheetItem = id; state.sheetQty = Math.max(1, cart()[id] || 1);
    const can = cat().accepting_orders && item.is_available;
    openSheet(plate(item) + '<h3 id="sheet-title">' + esc(L(item.names)) + "</h3>" + (L(item.descriptions) ? '<p class="desc">' + esc(L(item.descriptions)) + "</p>" : "") +
      '<p class="dish-price num" style="margin-top:10px;font-size:19px">' + money(item.price_minor) + "</p>" + (can
        ? '<div class="sheet-row"><div class="mini-stepper"><button data-sq="-1" aria-label="' + esc(t("less")) + '">' + I.minus + '</button><b class="num" id="sq"></b><button data-sq="1" aria-label="' +
          esc(t("more")) + '">' + I.plus + '</button></div><button class="cta" data-act="sheet-add" id="sheet-add"></button></div>'
        : '<div class="notice" style="margin-top:16px">' + I.info + "<b>" + esc(item.is_available ? tc("closed") : t("sold_out")) + "</b><span>" + esc(t("closed_note")) + "</span></div>"));
    updateSheet();
  }
  function updateSheet() {
    const button = $("#sheet-add");
    if (!button) return;
    const item = itemsOf(cat())[state.sheetItem];
    $("#sq").textContent = state.sheetQty;
    button.innerHTML = "<span>" + esc(cart()[item.id] ? t("update") : t("add")) + '</span><span class="num">' + money(item.price_minor * state.sheetQty) + "</span>";
  }

  // ---------- действия ----------
  function setQty(id, qty) {
    if (qty > 0) cart()[id] = Math.min(qty, 20); else delete cart()[id];
    state.key = null; save(); render();
  }
  async function next() {
    state.sending = true; render();
    const r = await api("/menu/quote", {method: "POST", body: JSON.stringify({catalog: state.catalog,
      lines: cartLines().map((l) => ({item_id: l.id, quantity: l.qty}))})});
    state.sending = false;
    if (r.ok) { state.quote = r.data; state.key = state.key || newKey(); go("checkout"); return; }
    await failed(r);
  }
  async function place() {
    const comment = ($("#comment") || {}).value || "";
    state.comments[state.catalog] = comment; save();
    state.sending = true; render();
    const r = await api("/orders", {method: "POST", body: JSON.stringify({catalog: state.catalog, client_order_key: state.key,
      language: state.lang, lines: state.quote.lines.map((l) => ({item_id: l.item_id, quantity: l.quantity})),
      comment: comment.trim() || null, expected_total_minor: state.quote.total_minor})});
    state.sending = false;
    if (r.ok) {
      state.carts[state.catalog] = {}; state.comments[state.catalog] = ""; state.key = null; state.quote = null; save();
      showOrder(r.data); refreshData(); return;
    }
    if (r.code === "ERR-ROOMSERVICE-005") {
      const q = await api("/menu/quote", {method: "POST", body: JSON.stringify({catalog: state.catalog,
        lines: state.quote.lines.map((l) => ({item_id: l.item_id, quantity: l.quantity}))})});
      if (q.ok) state.quote = q.data;
      toast(t("err_total")); render(); return;
    }
    await failed(r);
  }
  async function failed(r) {
    const known = {"ERR-ROOMSERVICE-003": ["err_closed", "menu"], "ERR-ROOMSERVICE-004": ["err_sold_out", "cart"],
      "ERR-ROOMSERVICE-001": ["err_sold_out", "cart"], "ERR-ROOMSERVICE-006": ["err_limit", null], "ERR-ROOMSERVICE-007": ["err_accepted", null]};
    const [message, screen] = known[r.code] || ["err_generic", null];
    toast(t(message));
    if (screen) { await loadData(); go(screen); } else render();
  }
  function showOrder(order) {
    state.order = order;
    history.replaceState(null, "", "#order=" + order.id);
    go("order");
    clearTimeout(state.poll);
    if (ACTIVE.includes(order.stage)) state.poll = setTimeout(refreshOrder, 8000);
  }
  async function refreshOrder() {
    if (state.screen !== "order" || !state.order) return;
    const r = await api("/orders/" + encodeURIComponent(state.order.id), {method: "GET"});
    if (r.ok && state.screen === "order") {
      state.order = r.data; render();
      if (ACTIVE.includes(r.data.stage)) state.poll = setTimeout(refreshOrder, 8000);
    } else if (!r.ok) state.poll = setTimeout(refreshOrder, 15000);
  }
  async function cancelOrder() {
    closeSheet();
    const r = await api("/orders/" + encodeURIComponent(state.order.id) + "/cancel", {method: "POST"});
    if (r.ok) { state.order = r.data; render(); refreshData(); return; }
    await failed(r);
    refreshOrder();
  }
  function toMenu() { clearTimeout(state.poll); history.replaceState(null, "", location.pathname); go("menu"); refreshData(); }
  // Свежие каталоги и заказы в фоне; на меню — перерисовать.
  async function refreshData() { if ((await loadData()) && state.screen === "menu") render(); }

  async function onClick(e) {
    const el = e.target.closest("[data-act],[data-add],[data-dec],[data-open],[data-chip],[data-tab],[data-lang],[data-order],[data-sq]");
    if (!el) { if (e.target === scrim) closeSheet(); return; }
    const d = el.dataset;
    if (d.add) setQty(d.add, (cart()[d.add] || 0) + 1);
    else if (d.dec) setQty(d.dec, (cart()[d.dec] || 0) - 1);
    else if (d.open) openItem(d.open);
    else if (d.tab) { state.catalog = d.tab; state.key = null; go("menu"); }
    else if (d.lang) { state.lang = d.lang; store.set("rs-lang", d.lang); render(); }
    else if (d.chip) { const sec = $("#sec-" + d.chip); if (sec) sec.scrollIntoView({behavior: "smooth"}); }
    else if (d.order) { const o = state.data.orders.find((x) => x.id === d.order); if (o) showOrder(o); }
    else if (d.sq) { state.sheetQty = Math.max(1, Math.min(20, state.sheetQty + Number(d.sq))); updateSheet(); }
    else if (d.act === "chat") location.href = base;
    else if (d.act === "menu") toMenu();
    else if (d.act === "cart") go("cart");
    else if (d.act === "clear") { state.carts[state.catalog] = {}; save(); go("menu"); }
    else if (d.act === "next") next();
    else if (d.act === "place") place();
    else if (d.act === "more") toMenu();
    else if (d.act === "sheet-add") { const id = state.sheetItem; closeSheet(); setQty(id, state.sheetQty); }
    else if (d.act === "ask-cancel") {
      openSheet('<h4 id="sheet-title">' + esc(t("cancel_ask", {n: state.order.number})) + '</h4><p class="sub">' + esc(tc("cancel_free")) +
        '</p><div class="actions"><button class="cta center danger" data-act="do-cancel">' + esc(t("cancel")) + '</button><button class="ghost-btn" data-act="close-sheet">' + esc(t("keep")) + "</button></div>");
    }
    else if (d.act === "do-cancel") cancelOrder();
    else if (d.act === "close-sheet") closeSheet();
  }

  // Подсветка чипса раздела, который сейчас на экране.
  let observer = null;
  function spy() {
    if (observer) observer.disconnect();
    if (!("IntersectionObserver" in window)) return;
    observer = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        for (const chip of document.querySelectorAll(".chip")) chip.setAttribute("aria-current", String(chip.dataset.chip === entry.target.dataset.section));
      }
    }, {rootMargin: "-130px 0px -60% 0px"});
    for (const sec of document.querySelectorAll(".section")) observer.observe(sec);
  }

  document.addEventListener("click", onClick);
  // Фото не загрузилось (хранилище недоступно) — убрать его, под ним плашка. error не всплывает.
  document.addEventListener("error", (e) => { if (e.target.matches && e.target.matches(".photo img")) e.target.remove(); }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSheet(); });
  (async () => {
    state.carts = store.get("rs-carts") || {}; state.comments = store.get("rs-comments") || {};
    const saved = store.get("rs-lang");
    state.lang = saved || ((navigator.languages || [navigator.language || "en"]).map((l) => String(l).slice(0, 2).toLowerCase()).find((l) => LANG_LABELS[l]) || "en");
    render();
    if (!(await loadData())) return;
    const match = location.hash.match(/^#order=([0-9a-f-]{36})$/);
    if (match) {
      const r = await api("/orders/" + match[1], {method: "GET"});
      if (r.ok) { showOrder(r.data); return; }
    }
    go("menu");
  })();
})();
