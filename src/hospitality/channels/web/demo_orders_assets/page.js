// Демо «Заказать в номер» (issue #438, дизайн — #440). Всё живёт в памяти
// страницы: ни запросов к нашему серверу, ни записи; обновление возвращает
// демо в начало. Всё, что приходит из данных, выводится через esc() или
// textContent — номер комнаты вносит персонал.
"use strict";
(() => {
  const DATA = JSON.parse(document.getElementById("demo-data").textContent);
  const $ = (selector, root = document) => root.querySelector(selector);
  const chatUrl = location.pathname.replace(/\/orders\/?$/, "");
  const view = $("#view"), bar = $("#bar"), seg = $("#seg"), sheet = $("#sheet"), scrim = $("#scrim");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const STEPS = ["accepted", "paid", "delivering"];
  const items = DATA.items;
  const sectionOf = (key) => DATA.sections.find((s) => s.key === key);

  function pickLang() {
    for (const l of navigator.languages || [navigator.language || ""]) {
      const code = String(l).slice(0, 2).toLowerCase();
      if (code === "kk" || code === "ru" || code === "en") return code;
    }
    return "en";
  }
  const state = {lang: pickLang(), section: "restaurant", screen: "menu", query: "", sending: false,
    carts: {restaurant: {}, shop: {}}, pay: {restaurant: "cash", shop: "cash"},
    notes: {restaurant: "", shop: ""}, orders: {restaurant: null, shop: null},
    next: {restaurant: 13, shop: 8}};

  // split/join, а не replace: «$&» в значении не должен стать шаблоном замены.
  function t(key, vars) {
    let s = DATA.texts[key][state.lang];
    for (const [k, v] of Object.entries(vars || {})) s = s.split("{" + k + "}").join(String(v));
    return s;
  }
  const L = (localized) => (localized ? localized[state.lang] : "");
  const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);
  const money = (n) => n.toLocaleString(state.lang === "en" ? "en-US" : "ru-RU") + " ₸";
  const clock = () => new Date().toTimeString().slice(0, 5);
  const cartOf = () => state.carts[state.section];
  const lines = () => Object.entries(cartOf()).filter(([, q]) => q > 0)
    .map(([key, qty]) => ({key, qty, item: items[key]}));
  const sum = (ls) => ls.reduce((a, l) => a + l.item.price * l.qty, 0);
  const count = (ls) => ls.reduce((a, l) => a + l.qty, 0);
  const haptic = () => { if (navigator.vibrate) navigator.vibrate(8); };

  const svg = (d, w) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' +
    (w || 2) + '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + "</svg>";
  const I = {
    back: svg('<path d="M15 5l-7 7 7 7"/>', 2.4), chev: svg('<path d="M9 5l7 7-7 7"/>', 2.4),
    plus: svg('<path d="M12 5v14M5 12h14"/>', 2.6), minus: svg('<path d="M5 12h14"/>', 2.6),
    x: svg('<path d="M6 6l12 12M18 6L6 18"/>', 2.4), check: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>', 2.8),
    search: svg('<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>'),
    clock: svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
    bag: svg('<path d="M5 8h14l-1.2 12.1a1 1 0 0 1-1 .9H7.2a1 1 0 0 1-1-.9z"/><path d="M9 8V6.5a3 3 0 0 1 6 0V8"/>'),
    door: svg('<path d="M6 21V4.5A1.5 1.5 0 0 1 7.5 3h9A1.5 1.5 0 0 1 18 4.5V21M4 21h16"/><circle cx="14.5" cy="12.5" r="1" fill="currentColor"/>'),
    cash: svg('<rect x="2.5" y="6" width="19" height="12" rx="2.5"/><circle cx="12" cy="12" r="2.5"/><path d="M6 9.5v5M18 9.5v5"/>'),
    card: svg('<rect x="2.5" y="5" width="19" height="14" rx="2.5"/><path d="M2.5 10h19M6.5 15h4"/>'),
    chat: svg('<path d="M4 5h16v11H9l-5 4z"/>'),
  };

  // ---------- кусочки разметки ----------
  function plate(key, size) {
    const i = items[key];
    const badge = i.badge && !size ? '<span class="badge badge-' + esc(i.badge) + '">' + esc(t("badge_" + i.badge)) + "</span>" : "";
    return '<span class="plate ' + (size || "") + '" style="--a:' + esc(i.colors[0]) + ";--b:" + esc(i.colors[1]) + '">' +
      '<span class="emo">' + esc(i.emoji) + "</span>" + badge + "</span>";
  }
  function control(key, fresh) {
    const q = cartOf()[key] || 0;
    if (!q) {
      return '<button type="button" class="add" data-add="' + key + '" aria-label="' + esc(t("add") + ": " + L(items[key].name)) + '">' + I.plus + "</button>";
    }
    return '<div class="stepper' + (fresh ? " grow" : "") + '"><button type="button" data-dec="' + key + '" aria-label="' + esc(t("remove")) + '">' + I.minus + "</button>" +
      '<b class="tick">' + q + '</b><button type="button" data-add="' + key + '" aria-label="' + esc(t("add")) + '">' + I.plus + "</button></div>";
  }
  function card(key, kind) {
    const i = items[key];
    return '<article class="card ' + (kind || "") + '" data-item="' + key + '" data-open="' + key + '">' +
      '<div class="card-media"><button type="button" class="card-open" data-open="' + key + '" aria-label="' + esc(L(i.name)) + '">' + plate(key) + "</button>" +
      '<div class="ctl" data-ctl="' + key + '">' + control(key) + "</div></div>" +
      '<div class="card-body"><span class="price">' + money(i.price) + '</span><h3 class="name">' + esc(L(i.name)) + "</h3>" +
      (i.meta ? '<span class="meta">' + esc(L(i.meta)) + "</span>" : "") + "</div></article>";
  }
  const info = (icon, title, sub) => '<div class="info"><span class="info-ico">' + icon + "</span><div><b>" + esc(title) + "</b><span>" + esc(sub) + "</span></div></div>";
  const stepText = (o) => esc(t("order_n", {n: o.number})) + " · " + esc(t("step_" + STEPS[o.step]));
  const etaText = (o) => esc(t("eta_status", {min: sectionOf(state.section).eta_steps[o.step]}));

  // ---------- экраны ----------
  function renderMenu() {
    const s = sectionOf(state.section), order = state.orders[state.section];
    let html = '<section class="hero hero-' + s.key + '"><h1>' + esc(t("hero_" + s.key)) + "</h1>" +
      '<div class="hero-chips"><span>' + I.clock + esc(t("eta_" + s.key)) + "</span><span>" + I.door + esc(t("to_door")) +
      "</span><span>" + I.cash + esc(t("payment")) + '</span></div><div class="hero-art" aria-hidden="true">' +
      s.art.map((e) => "<span>" + esc(e) + "</span>").join("") + "</div></section>";
    if (order) {
      html += '<button type="button" class="order-pill" data-go="order"><span class="live"></span><span class="order-pill-text"><b>' +
        stepText(order) + "</b><small>" + etaText(order) + "</small></span>" + I.chev + "</button>";
    }
    html += '<label class="search">' + I.search + '<input id="q" type="search" enterkeyhint="search" autocomplete="off" placeholder="' +
      esc(t("search_" + s.key)) + '" value="' + esc(state.query) + '"><button type="button" class="search-clear" data-clear-q aria-label="' +
      esc(t("clear")) + '">' + I.x + "</button></label>";
    html += '<div id="popular"><h2 class="h2">' + esc(t("popular")) + '</h2><div class="rail">' + s.popular.map((k) => card(k, "wide")).join("") + "</div></div>";
    html += '<nav class="chips" id="chips">' + s.categories.map((c, n) => '<button type="button" class="chip" data-chip="' + n + '">' + esc(L(c.title)) + "</button>").join("") + "</nav>";
    html += s.categories.map((c, n) => '<section class="cat" id="cat-' + n + '" data-cat="' + n + '"><h2 class="h2">' + esc(L(c.title)) +
      '</h2><div class="grid">' + c.items.map((k) => card(k)).join("") + "</div></section>").join("");
    html += '<div class="empty" id="empty" hidden><span>🔎</span>' + esc(t("not_found")) + "</div>";
    view.innerHTML = html;
    applyQuery();
    spy();
  }

  function renderCart() {
    const s = state.section, ls = lines();
    if (!ls.length) { state.screen = "menu"; renderMenu(); return; }
    const ups = sectionOf(s).upsell.filter((k) => !cartOf()[k]);
    view.innerHTML = '<div class="screen-head"><h1>' + esc(t("cart_" + s)) + '</h1><button type="button" class="link danger" data-go="clear">' + esc(t("clear")) + "</button></div>" +
      '<div class="panel">' + ls.map((l) => '<div class="line">' + plate(l.key, "mini") + '<div class="line-text"><b>' + esc(L(l.item.name)) + "</b><span>" +
        money(l.item.price) + '</span></div><div class="ctl ctl-inline" data-ctl="' + l.key + '">' + control(l.key) + "</div></div>").join("") + "</div>" +
      (ups.length ? '<h2 class="h2">' + esc(t("add_more")) + '</h2><div class="rail">' + ups.map((k) => card(k, "mini")).join("") + "</div>" : "") +
      '<div class="panel">' + info(I.door, t("deliver_to", {room: DATA.room}), t("by_code")) + info(I.clock, t("asap"), t("eta_" + s)) + "</div>" +
      '<h2 class="h2">' + esc(t("payment")) + '</h2><div class="pay">' + ["cash", "card"].map((m) => '<button type="button" class="pay-opt" data-pay="' + m +
        '" aria-pressed="' + (m === state.pay[s]) + '">' + I[m] + "<span>" + esc(t(m)) + "</span><i>" + I.check + "</i></button>").join("") + "</div>" +
      '<h2 class="h2">' + esc(t("notes")) + '</h2><textarea id="note" maxlength="200" placeholder="' + esc(t("optional")) + '">' + esc(state.notes[s]) + "</textarea>" +
      '<div class="panel total"><span>' + esc(t("total")) + "</span><b>" + money(sum(ls)) + "</b></div>";
  }

  function renderOrder() {
    const s = state.section, o = state.orders[s];
    if (!o) { state.screen = "menu"; renderMenu(); return; }
    const texts = [t("text_accepted", {total: money(o.total), method: t("method_" + o.pay)}),
      t("text_paid_" + s, {room: DATA.room}), t("text_delivering", {room: DATA.room})];
    const icons = ["✅", s === "restaurant" ? "👨‍🍳" : "📦", "🛎️"];
    const last = o.step === STEPS.length - 1;
    view.innerHTML = '<div class="status"><div class="status-ico' + (last ? " done" : "") + '">' + icons[o.step] + "</div>" +
      '<p class="status-no">' + esc(t("order_n", {n: o.number})) + "</p><h1>" + esc(t("step_" + STEPS[o.step])) + "</h1>" +
      '<p class="status-text">' + esc(texts[o.step]) + '</p><span class="eta">' + I.clock + etaText(o) + "</span>" +
      '<div class="progress">' + STEPS.map((k, i) => '<i class="' + (i < o.step ? "done" : i === o.step ? "now" : "") + '"></i>').join("") + "</div></div>" +
      '<ol class="panel timeline">' + STEPS.map((k, i) => '<li class="' + (i <= o.step ? "done" : "") + '"><span class="dot">' + (i <= o.step ? I.check : i + 1) +
        "</span><b>" + esc(t("step_" + k)) + "</b><time>" + (o.times[i] || "") + "</time></li>").join("") + "</ol>" +
      '<h2 class="h2">' + esc(t("items")) + '</h2><div class="panel">' + o.lines.map((l) => '<div class="line">' + plate(l.key, "mini") +
        '<div class="line-text"><b>' + esc(L(l.item.name)) + "</b><span>×" + l.qty + '</span></div><b class="line-sum">' + money(l.item.price * l.qty) + "</b></div>").join("") +
      '<div class="total-row"><span>' + esc(t("total")) + "</span><b>" + money(o.total) + "</b></div></div>" +
      '<div class="actions">' + (last ? "" : '<button type="button" class="demo-next" data-go="next">' + esc(t("demo_next")) + " ›</button>") +
      '<button type="button" class="ghost" data-go="more">' + esc(t("order_more")) + '</button><button type="button" class="ghost" data-go="chat">' +
      I.chat + esc(t("to_chat")) + "</button></div>";
  }

  function renderBar(mode) {
    const ls = lines();
    if (state.screen === "menu" && ls.length) {
      const appear = bar.hidden ? " rise" : mode === "bump" ? " bump" : "";
      bar.innerHTML = '<button type="button" class="cartbar' + appear + '" data-go="cart"><span class="cartbar-ico">' + I.bag + "<b>" + count(ls) + "</b></span>" +
        '<span class="cartbar-text"><b>' + esc(t("cart_" + state.section)) + "</b><small>" + esc(t("eta_" + state.section)) + "</small></span>" +
        '<span class="cartbar-sum">' + money(sum(ls)) + "</span></button>";
      bar.hidden = false;
    } else if (state.screen === "cart" && ls.length) {
      bar.innerHTML = state.sending ? '<button type="button" class="cta" disabled><span class="spin"></span>' + esc(t("sending")) + "</button>"
        : '<button type="button" class="cta" data-go="place">' + esc(t("place_order", {total: money(sum(ls))})) + "</button>";
      bar.hidden = false;
    } else {
      bar.hidden = true; bar.innerHTML = "";
    }
  }

  function render() {
    document.documentElement.lang = state.lang;
    for (const el of document.querySelectorAll("[data-t]")) el.textContent = t(el.dataset.t);
    for (const b of document.querySelectorAll("[data-lang]")) b.setAttribute("aria-pressed", String(b.dataset.lang === state.lang));
    $("#room").textContent = "№ " + DATA.room;
    $("#back").innerHTML = I.back;
    $("#back").setAttribute("aria-label", t("back"));
    seg.hidden = state.screen !== "menu";
    seg.dataset.active = state.section;
    for (const b of seg.querySelectorAll("[data-seg]")) {
      b.setAttribute("aria-selected", String(b.dataset.seg === state.section));
      $("span", b).textContent = t("tab_" + b.dataset.seg);
    }
    if (state.screen === "cart") renderCart();
    else if (state.screen === "order") renderOrder();
    else renderMenu();
    renderBar();
  }
  function go(screen) { state.screen = screen; render(); window.scrollTo(0, 0); }

  // ---------- поиск и подсветка категорий ----------
  const hay = (key) => [items[key].name.ru, items[key].name.kk, items[key].name.en, L(items[key].desc)].join(" ").toLowerCase();
  function applyQuery() {
    const q = state.query.trim().toLowerCase();
    $("#popular").hidden = !!q; $("#chips").hidden = !!q; $("[data-clear-q]").hidden = !q;
    let any = false;
    for (const sec of view.querySelectorAll(".cat")) {
      let shown = 0;
      for (const c of sec.querySelectorAll(".card")) { c.hidden = !!q && !hay(c.dataset.item).includes(q); if (!c.hidden) shown++; }
      sec.hidden = !shown; any = any || shown > 0;
    }
    $("#empty").hidden = any;
  }
  let observer = null;
  function spy() {
    if (observer) observer.disconnect();
    setChip("0");
    if (!("IntersectionObserver" in window)) return;
    const seen = new Map();
    observer = new IntersectionObserver((entries) => {
      for (const e of entries) seen.set(e.target.dataset.cat, e.isIntersecting);
      const first = Array.from(view.querySelectorAll(".cat")).find((c) => seen.get(c.dataset.cat));
      if (first) setChip(first.dataset.cat);
    }, {rootMargin: "-130px 0px -55% 0px"});
    view.querySelectorAll(".cat").forEach((c) => observer.observe(c));
  }
  function setChip(n) {
    for (const c of view.querySelectorAll(".chip")) {
      const on = c.dataset.chip === n;
      if (on && c.getAttribute("aria-current") !== "true") {
        c.parentElement.scrollTo({left: c.offsetLeft - 16, behavior: reduceMotion ? "auto" : "smooth"});
      }
      c.setAttribute("aria-current", String(on));
    }
  }

  // ---------- корзина и анимации ----------
  function change(key, delta, source) {
    const cart = cartOf(), was = cart[key] || 0;
    cart[key] = Math.max(0, was + delta);
    if (!cart[key]) delete cart[key];
    if (delta > 0) haptic();
    if (state.screen === "cart") { renderCart(); renderBar(); return; }
    for (const c of document.querySelectorAll('[data-ctl="' + key + '"]')) c.innerHTML = control(key, was === 0);
    renderBar();
    if (delta > 0) fly(source, key);
  }
  function fly(source, key) {
    const target = $(".cartbar-ico", bar);
    if (reduceMotion || !source || !target) return;
    const a = source.getBoundingClientRect(), b = target.getBoundingClientRect();
    const dot = document.createElement("span");
    dot.className = "fly"; dot.textContent = items[key].emoji;
    dot.style.left = a.left + a.width / 2 - 16 + "px"; dot.style.top = a.top + a.height / 2 - 16 + "px";
    document.body.appendChild(dot);
    const dx = b.left + b.width / 2 - (a.left + a.width / 2), dy = b.top + b.height / 2 - (a.top + a.height / 2);
    dot.animate([{transform: "translate(0, 0) scale(1)"},
      {transform: "translate(" + dx * 0.4 + "px, " + (dy * 0.4 - 90) + "px) scale(1.25)", offset: 0.45},
      {transform: "translate(" + dx + "px, " + dy + "px) scale(.35)", opacity: 0.3}],
    {duration: 620, easing: "cubic-bezier(.45, 0, .65, 1)"}).onfinish = () => { dot.remove(); renderBar("bump"); };
  }
  function confetti() {
    if (reduceMotion) return;
    const colors = ["#0d7a54", "#f2a900", "#ff5b3a", "#2f5fd0", "#e64980"];
    for (let n = 0; n < 56; n++) {
      const p = document.createElement("i");
      p.className = "confetti"; p.style.background = colors[n % colors.length];
      p.style.left = 50 + (Math.random() - 0.5) * 20 + "vw"; p.style.top = "34vh";
      document.body.appendChild(p);
      const dx = (Math.random() - 0.5) * 100, up = 15 + Math.random() * 25, turn = Math.random() * 900 - 450;
      p.animate([{transform: "translate(0, 0) rotate(0deg)"},
        {transform: "translate(" + dx * 0.6 + "vw, -" + up + "vh) rotate(" + turn / 2 + "deg)", offset: 0.3},
        {transform: "translate(" + dx + "vw, 70vh) rotate(" + turn + "deg)", opacity: 0}],
      {duration: 1500 + Math.random() * 700, easing: "cubic-bezier(.2, .6, .4, 1)"}).onfinish = () => p.remove();
    }
  }
  function place() {
    const s = state.section, ls = lines();
    state.sending = true; renderBar();
    setTimeout(() => {
      state.orders[s] = {number: state.next[s]++, lines: ls, total: sum(ls), pay: state.pay[s], step: 0, times: [clock()]};
      state.carts[s] = {}; state.notes[s] = ""; state.sending = false;
      go("order");
    }, reduceMotion ? 150 : 900);
  }

  // ---------- лист товара ----------
  let sheetKey = null, sheetQty = 1, dragFrom = null;
  function openSheet(key) {
    const i = items[key];
    sheetKey = key; sheetQty = 1;
    sheet.innerHTML = '<div class="grab" aria-hidden="true"></div>' + plate(key, "big") + '<div class="sheet-body"><h2 id="sheet-title">' + esc(L(i.name)) + "</h2>" +
      (i.meta ? '<span class="meta">' + esc(L(i.meta)) + "</span>" : "") + '<p class="desc">' + esc(L(i.desc)) + "</p>" +
      '<div class="sheet-row"><div class="qty"><button type="button" data-sq="-1" aria-label="' + esc(t("remove")) + '">' + I.minus + '</button><b id="sq"></b>' +
      '<button type="button" data-sq="1" aria-label="' + esc(t("add")) + '">' + I.plus + '</button></div><button type="button" class="cta" data-go="sheet-add" id="sheet-add"></button></div></div>';
    sheet.setAttribute("aria-labelledby", "sheet-title");
    updateSheet();
    sheet.hidden = false; scrim.hidden = false; document.body.classList.add("locked");
    requestAnimationFrame(() => requestAnimationFrame(() => { sheet.classList.add("open"); scrim.classList.add("open"); }));
  }
  function updateSheet() {
    $("#sq").textContent = sheetQty;
    $("#sheet-add").textContent = t("add_n", {total: money(items[sheetKey].price * sheetQty)});
  }
  function closeSheet() {
    sheetKey = null; sheet.style.transform = "";
    sheet.classList.remove("open"); scrim.classList.remove("open"); document.body.classList.remove("locked");
    setTimeout(() => { if (!sheetKey) { sheet.hidden = true; scrim.hidden = true; } }, reduceMotion ? 0 : 380);
  }
  sheet.addEventListener("touchstart", (e) => { dragFrom = sheet.scrollTop <= 0 ? e.touches[0].clientY : null; }, {passive: true});
  sheet.addEventListener("touchmove", (e) => {
    if (dragFrom === null) return;
    const d = e.touches[0].clientY - dragFrom;
    if (d > 0) { sheet.style.transition = "none"; sheet.style.transform = "translateY(" + d + "px)"; }
  }, {passive: true});
  sheet.addEventListener("touchend", (e) => {
    if (dragFrom === null) return;
    const d = e.changedTouches[0].clientY - dragFrom;
    dragFrom = null; sheet.style.transition = ""; sheet.style.transform = "";
    if (d > 90) closeSheet();
  });
  scrim.addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && sheetKey) closeSheet(); });

  // ---------- события ----------
  document.addEventListener("input", (e) => {
    if (e.target.id === "q") { state.query = e.target.value; applyQuery(); }
    if (e.target.id === "note") state.notes[state.section] = e.target.value;
  });
  document.addEventListener("click", (e) => {
    const el = e.target.closest("button, .ctl, [data-open]");
    if (!el || (el.classList.contains("ctl") && !el.matches("button"))) return;
    const d = el.dataset;
    if (d.lang) { state.lang = d.lang; render(); return; }
    if (d.seg) {
      if (d.seg !== state.section) { state.section = d.seg; state.query = ""; go("menu"); }
      return;
    }
    if (d.add) { change(d.add, 1, el); return; }
    if (d.dec) { change(d.dec, -1); return; }
    if (d.sq) { sheetQty = Math.min(20, Math.max(1, sheetQty + Number(d.sq))); updateSheet(); return; }
    if (d.chip) { $("#cat-" + d.chip).scrollIntoView({behavior: reduceMotion ? "auto" : "smooth", block: "start"}); return; }
    if (d.pay) {
      state.pay[state.section] = d.pay;
      for (const b of view.querySelectorAll("[data-pay]")) b.setAttribute("aria-pressed", String(b.dataset.pay === d.pay));
      return;
    }
    if ("clearQ" in d) { state.query = ""; $("#q").value = ""; applyQuery(); $("#q").focus(); return; }
    if (d.open) { openSheet(d.open); return; }
    if (el.id === "back") { if (state.screen === "menu") location.href = chatUrl; else go("menu"); return; }
    const o = state.orders[state.section];
    if (d.go === "cart" || d.go === "order") go(d.go);
    else if (d.go === "clear") { state.carts[state.section] = {}; go("menu"); }
    else if (d.go === "place" && !state.sending) place();
    else if (d.go === "next" && o) { o.step += 1; o.times[o.step] = clock(); render(); if (o.step === STEPS.length - 1) confetti(); }
    else if (d.go === "more") { state.orders[state.section] = null; go("menu"); }
    else if (d.go === "chat") location.href = chatUrl;
    else if (d.go === "sheet-add" && sheetKey) { const key = sheetKey; change(key, sheetQty, $(".plate", sheet)); closeSheet(); }
  });
  const top = $("#top");
  window.addEventListener("scroll", () => top.classList.toggle("scrolled", window.scrollY > 4), {passive: true});
  render();
})();
