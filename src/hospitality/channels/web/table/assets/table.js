// «Забронировать столик» (spec 0064 §2.2, §5). Данные — JSON `…/table/data`, запрос —
// `POST …/table/bookings`, отмена — `…/bookings/{id}/cancel`. Всё, что пришло с сервера,
// выводится через esc(): названия ресторанов вносит отель. «Забронировано» — только
// стадия `confirmed`: до ответа ресторана это запрос.
"use strict";
(() => {
  const T = JSON.parse(document.getElementById("table-texts").textContent);
  const $ = (s, r) => (r || document).querySelector(s);
  const base = location.pathname.replace(/\/table\/?$/, "");
  const app = $("#app"), bar = $("#bar"), barInner = $("#bar-inner");
  const sheet = $("#sheet"), scrim = $("#scrim"), toastEl = $("#toast");
  const LANGS = ["kk", "ru", "en"], LANG_LABELS = {kk: "ҚАЗ", ru: "РУС", en: "ENG"};
  const state = {lang: "en", data: null, restaurant: null, date: null, time: null, party: 2,
    name: "", wishes: "", key: null, sending: false, poll: null, cancelId: null};

  const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);
  function t(key, vars) {
    const entry = T[key];
    let s = entry ? (entry[state.lang] || entry.en) : key;
    for (const [k, v] of Object.entries(vars || {})) s = s.split("{" + k + "}").join(String(v));
    return s;
  }
  const L = (text) => (text ? text[state.lang] || text.en : "");
  const svg = (d, w) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' + (w || 2) +
    '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + "</svg>";
  const I = {back: svg('<path d="M15 5l-7 7 7 7"/>', 2.2), plus: svg('<path d="M12 5v14M5 12h14"/>', 2.4),
    minus: svg('<path d="M5 12h14"/>', 2.4)};
  const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(36).slice(2)).replace(/[^A-Za-z0-9_-]/g, "");
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
    return {ok: response.ok, status: response.status, code: data && data.error ? data.error.code : null, data};
  }
  // Дата — календарный день отеля (`YYYY-MM-DD`), а не момент: форматируем в UTC.
  function dayLabel(iso, short) {
    const [y, m, d] = iso.split("-").map(Number);
    const [ty, tm, td] = state.data.today.split("-").map(Number);
    const shift = Math.round((Date.UTC(y, m - 1, d) - Date.UTC(ty, tm - 1, td)) / 86400000);
    if (shift === 0) return t("today");
    if (shift === 1) return t("tomorrow");
    const options = short ? {weekday: "short", day: "numeric", month: "short", timeZone: "UTC"} : {day: "numeric", month: "long", timeZone: "UTC"};
    try { return new Intl.DateTimeFormat(state.lang, options).format(new Date(Date.UTC(y, m - 1, d))); }
    catch (e) { return iso; }
  }
  const restaurant = () => state.data.restaurants.find((r) => r.key === state.restaurant);
  const day = () => (restaurant() ? restaurant().days.find((d) => d.date === state.date) : null);

  async function loadData() {
    const r = await api("/table/data", {method: "GET"});
    if (!r.ok) { if (r.status === 404) location.href = base; return false; }
    state.data = r.data;
    const rs = state.data.restaurants;
    if (!rs.some((x) => x.key === state.restaurant)) state.restaurant = rs.length ? rs[0].key : null;
    const days = restaurant() ? restaurant().days : [];
    if (!days.some((d) => d.date === state.date)) state.date = days.length ? days[0].date : null;
    if (!day() || !day().slots.includes(state.time)) state.time = null;
    if (restaurant()) state.party = Math.min(state.party, restaurant().max_party);
    return true;
  }

  // ---------- разметка ----------
  function topbar() {
    return '<header class="topbar"><button class="icon-btn" data-act="chat" aria-label="' + esc(t("back_chat")) + '">' + I.back +
      '</button><div class="topbar-title"><b>' + esc(t("title")) + "</b><span>" + esc(t("room", {room: state.data.room_number})) +
      '</span></div><div class="langs" role="group" aria-label="Language">' + LANGS.map((l) =>
      '<button data-lang="' + l + '" aria-pressed="' + (l === state.lang) + '">' + LANG_LABELS[l] + "</button>").join("") + "</div></header>";
  }
  function stageText(b) {
    if (b.stage === "cancelled") return b.answer === "confirmed" ? t("cancelled_after_confirm") : t("stage_cancelled");
    if (b.stage === "other_time") return t("stage_other_time", {time: b.proposed_time});
    return t("stage_" + b.stage);
  }
  function bookingCard(b) {
    let actions = "";
    if (b.stage === "other_time") actions += '<button class="cta center" data-alt="' + esc(b.id) + '">' + esc(t("request_at", {time: b.proposed_time})) + "</button>";
    if (b.cancellable) actions += '<button class="ghost-btn danger" data-cancel="' + esc(b.id) + '">' + esc(t(b.stage === "confirmed" ? "cancel_booking" : "cancel_request")) + "</button>";
    return '<article class="booking"><div class="booking-head"><b>' + esc(L(b.restaurant_names)) + '</b><span class="booking-when num">' +
      esc(dayLabel(b.local_date) + ", " + b.local_time) + '</span></div><span class="booking-when">' + esc(t("party_value", {n: b.party_size})) +
      '</span><span class="stage ' + esc(b.stage) + '"><span class="dot"></span>' + esc(stageText(b)) + "</span>" +
      (actions ? '<div class="actions">' + actions + "</div>" : "") + "</article>";
  }
  function choice(attr, value, label, sub, pressed) {
    return '<button class="choice" ' + attr + '="' + esc(value) + '" aria-pressed="' + pressed + '">' + esc(label) +
      (sub ? "<small>" + esc(sub) + "</small>" : "") + "</button>";
  }
  function form() {
    const r = restaurant();
    if (!r) return "";
    let h = '<div class="field"><p class="label">' + esc(t("field_restaurant")) + '</p><div class="choice-row">' +
      state.data.restaurants.map((x) => choice("data-restaurant", x.key, L(x.names), L(x.note), x.key === state.restaurant)).join("") + "</div></div>";
    h += '<div class="field"><p class="label">' + esc(t("field_date")) + "</p>";
    h += r.days.length ? '<div class="choice-row">' + r.days.map((d) => choice("data-date", d.date, dayLabel(d.date, true), "", d.date === state.date)).join("") + "</div>"
      : '<p class="hint">' + esc(t("no_slots")) + "</p>";
    h += "</div>";
    if (day()) {
      h += '<div class="field"><p class="label">' + esc(t("field_time")) + '</p><div class="slots">' +
        day().slots.map((s) => choice("data-time", s, s, "", s === state.time)).join("") + "</div></div>";
    }
    h += '<div class="field"><p class="label">' + esc(t("field_party")) + '</p><div class="party"><b class="num">' + state.party +
      '</b><div class="mini-stepper"><button data-act="less" aria-label="−">' + I.minus + '</button><button data-act="more" aria-label="+">' + I.plus +
      "</button></div></div>" + (state.party >= r.max_party ? '<p class="hint">' + esc(t("party_over", {n: r.max_party})) + "</p>" : "") + "</div>";
    h += '<div class="field"><p class="label">' + esc(t("field_name")) + '</p><input class="text" id="name" maxlength="60" autocomplete="name" value="' + esc(state.name) + '"></div>';
    h += '<div class="field"><p class="label">' + esc(t("field_wishes")) + '</p><textarea id="wishes" maxlength="300" placeholder="' +
      esc(t("wishes_placeholder")) + '">' + esc(state.wishes) + "</textarea></div>";
    return '<section class="card card-pad">' + h + "</section>";
  }
  function bookingsHtml() {
    const bookings = state.data.bookings;
    return bookings.length ? '<p class="label">' + esc(t("my_bookings")) + "</p>" + bookings.map(bookingCard).join("") : "";
  }
  function renderBar() {
    const ready = restaurant() && state.date && state.time && !state.sending;
    bar.hidden = !restaurant();
    barInner.innerHTML = '<button class="cta center" data-act="send"' + (ready ? "" : " disabled") + ">" +
      (state.sending ? '<span class="spin"></span>' : esc(t("submit"))) + '</button><p class="bar-note">' + esc(t("submit_note")) + "</p>";
  }
  function render() {
    document.documentElement.lang = state.lang;
    if (!state.data) { app.innerHTML = '<div class="loading"><span class="spin"></span></div>'; return; }
    app.innerHTML = topbar() + '<div id="bookings">' + bookingsHtml() + '</div><div class="screen-head"></div>' + form();
    renderBar();
    schedulePoll();
  }
  // Опрос стадии раз в 8 с (§2.2), пока есть запрос без ответа; форму не перерисовывает —
  // гость в ней печатает имя, — разве что выбранный слот успел пройти.
  function schedulePoll() {
    clearTimeout(state.poll);
    if (state.data && state.data.bookings.some((b) => b.stage === "waiting")) state.poll = setTimeout(poll, 8000);
  }
  async function poll() {
    const time = state.time;
    if (!(await loadData())) { state.poll = setTimeout(poll, 15000); return; }
    const list = $("#bookings");
    if (state.time !== time || !list) { render(); return; }
    list.innerHTML = bookingsHtml(); renderBar(); schedulePoll();
  }
  async function refresh() { if (await loadData()) render(); }

  // ---------- действия ----------
  async function send(body) {
    state.sending = true; render();
    const r = await api("/table/bookings", {method: "POST", body: JSON.stringify(Object.assign({language: state.lang}, body))});
    state.sending = false;
    if (r.ok) { toast(t("sent")); return true; }
    toast(r.status === 429 ? t("err_limit") : r.status === 422 ? t("err_invalid") : r.status === 404 ? t("err_invalid") : t("err_network"));
    await refresh();
    return false;
  }
  async function submit() {
    if (!state.key) state.key = newKey();
    const ok = await send({client_key: state.key, restaurant_key: state.restaurant, date: state.date, time: state.time,
      party_size: state.party, guest_name: state.name || null, wishes: state.wishes || null});
    if (ok) { state.key = null; state.time = null; state.wishes = ""; await refresh(); window.scrollTo(0, 0); }
    else render();
  }
  // «Запросить на 20:30» (§2.3): новый запрос с тем же рестораном, датой и гостями; ключ — от брони.
  async function requestAlt(id) {
    const b = state.data.bookings.find((x) => x.id === id);
    if (!b) return;
    await send({client_key: "alt" + id.replace(/-/g, ""), restaurant_key: b.restaurant_key, date: b.local_date,
      time: b.proposed_time, party_size: b.party_size});
    await refresh();
  }
  function openSheet(html) {
    sheet.innerHTML = '<div class="grabber"></div>' + html; sheet.hidden = false; scrim.hidden = false;
    requestAnimationFrame(() => { sheet.classList.add("open"); scrim.classList.add("open"); });
  }
  function closeSheet() { sheet.classList.remove("open"); scrim.classList.remove("open"); sheet.hidden = true; scrim.hidden = true; state.cancelId = null; }
  function askCancel(id) {
    const b = state.data.bookings.find((x) => x.id === id);
    if (!b) return;
    state.cancelId = id;
    const label = t(b.stage === "confirmed" ? "cancel_booking" : "cancel_request");
    openSheet('<h4 id="sheet-title">' + esc(L(b.restaurant_names)) + '</h4><p class="sub">' + esc(dayLabel(b.local_date) + ", " + b.local_time + " · " + t("party_value", {n: b.party_size})) +
      '</p><div class="actions"><button class="cta center danger" data-act="do-cancel">' + esc(label) + '</button><button class="ghost-btn" data-act="close-sheet">' + esc(t("keep")) + "</button></div>");
  }
  async function doCancel() {
    const id = state.cancelId;
    closeSheet();
    if (!id) return;
    const r = await api("/table/bookings/" + encodeURIComponent(id) + "/cancel", {method: "POST"});
    if (!r.ok) toast(r.status === 409 ? t("err_cancel") : t("err_network"));
    await refresh();
  }

  document.addEventListener("click", (event) => {
    const el = event.target.closest("button, [data-act]");
    if (!el) return;
    const d = el.dataset;
    if (d.lang) { state.lang = d.lang; try { sessionStorage.setItem("tb-lang", d.lang); } catch (e) { /* без хранилища */ } render(); }
    else if (d.restaurant) {
      state.restaurant = d.restaurant; state.time = null;
      const r = restaurant(); state.date = r.days.length ? r.days[0].date : null; state.party = Math.min(state.party, r.max_party); render();
    }
    else if (d.date) { state.date = d.date; state.time = null; render(); }
    else if (d.time) { state.time = d.time; render(); }
    else if (d.alt) requestAlt(d.alt);
    else if (d.cancel) askCancel(d.cancel);
    else if (d.act === "less") { state.party = Math.max(1, state.party - 1); render(); }
    else if (d.act === "more") { state.party = Math.min(restaurant().max_party, state.party + 1); render(); }
    else if (d.act === "send") submit();
    else if (d.act === "do-cancel") doCancel();
    else if (d.act === "close-sheet") closeSheet();
    else if (d.act === "chat") location.href = base;
  });
  scrim.addEventListener("click", closeSheet);
  document.addEventListener("input", (event) => {
    if (event.target.id === "name") state.name = event.target.value;
    else if (event.target.id === "wishes") state.wishes = event.target.value;
  });

  let saved = null;
  try { saved = sessionStorage.getItem("tb-lang"); } catch (e) { /* без хранилища */ }
  state.lang = LANGS.includes(saved) ? saved
    : ((navigator.languages || [navigator.language || "en"]).map((l) => String(l).slice(0, 2).toLowerCase()).find((l) => LANGS.includes(l)) || "en");
  render();
  refresh();
})();
