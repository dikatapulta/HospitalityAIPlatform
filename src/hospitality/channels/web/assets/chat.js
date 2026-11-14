"use strict";
// Страница веб-чата (spec 0027 §3.4, вид — #746). Вклеивается в page.py при импорте;
// маркер ниже заменяется скриптом непрочитанного (assets/unread.js, #223).
const base = location.pathname.replace(/\/$/, "");
const room = decodeURIComponent(base.split("/").pop());
// Адрес страницы комнаты того же отеля: гость исправил комнату на экране входа (spec 0068).
const roomBase = (r) => base.slice(0, base.lastIndexOf("/") + 1) + encodeURIComponent(r);
const $ = (id) => document.getElementById(id);
// Шапка и строка ввода стоят на месте, прокручивается только лента (spec 0068 §2.3).
const app = document.querySelector(".app");
const scroller = $("scroller");
const log = $("log");
const gate = $("gate");
const composer = $("composer");
const input = $("text");
const send = $("send");
const welcome = $("welcome");
const langBtn = $("lang");
const langMenu = $("lang-menu");
const T = JSON.parse($("chat-texts").textContent);
const LANGS = ["kk", "ru", "en"];
const LANG_KEY = "necturn:chat-lang";
// Курсор опроса — id последнего сообщения, принесённого опросом; двигает его только опрос (#117).
let lastId = null;
let polling = null;
let lastDay = null;
// Что уже стоит в ленте (#117): id сообщений сервера и client_message_id своих сообщений гостя —
// их страница рисует до ответа сервера. Опрос и ответ отправки приносят одно и то же в любом
// порядке, каждое рисуется один раз.
const shown = new Set();
// Последняя недоставленная отправка: повтор того же текста идёт с тем же client_message_id —
// дошедшая первый раз не станет вторым платным ходом (P-8, #117).
let retry = null;

// Язык надписей после входа: выбор гостя в шапке, иначе язык телефона. Экран входа —
// на трёх языках всегда: до входа язык гостя неизвестен (spec 0029).
let lang = (() => {
  try { const saved = sessionStorage.getItem(LANG_KEY); if (LANGS.includes(saved)) return saved; } catch (e) { /* без хранилища */ }
  const code = (navigator.language || "").toLowerCase().split("-")[0];
  return LANGS.includes(code) ? code : "en";
})();
const t = (key) => T[key][lang];
const roomLine = (l, r) => T.room[l].replace("{}", r);
const locale = () => (lang === "kk" ? "kk-KZ" : lang);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}
function scrollDown() { scroller.scrollTop = scroller.scrollHeight; }
function atEnd() { return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48; }

// Колонка — на видимой области экрана: клавиатура iPhone сжимает её, строка ввода остаётся над
// клавиатурой, шапка — наверху. При увеличении щипком колонка возвращается к целому экрану.
const vv = window.visualViewport;
function fit() {
  const end = atEnd();
  const zoomed = vv.scale > 1.01;
  app.style.height = zoomed ? "" : vv.height + "px";
  app.style.bottom = zoomed ? "" : "auto";
  app.style.transform = zoomed ? "" : "translateY(" + vv.offsetTop + "px)";
  if (end) scrollDown();
}
if (vv) { vv.addEventListener("resize", fit); vv.addEventListener("scroll", fit); fit(); }
// Отступы ленты под «стеклом» — высота шапки и строки ввода; та растёт (плашки, пауза лимита,
// подсказка скрепки) — кто был у конца ленты, остаётся у конца.
if (window.ResizeObserver) {
  const topbar = document.querySelector(".topbar");
  const bars = new ResizeObserver(() => {
    const end = atEnd();
    app.style.setProperty("--top-h", topbar.offsetHeight + "px");
    app.style.setProperty("--bottom-h", composer.offsetHeight + "px");
    if (end) scrollDown();
  });
  bars.observe(topbar); bars.observe(composer);
}

// «Печатает»: три точки в ленте, пока ход гостя ждёт ответа.
const typingRow = el("div", "msg outbound typing");
typingRow.setAttribute("role", "status");
for (let i = 0; i < 3; i += 1) typingRow.appendChild(el("i"));
function typing(on) {
  typingRow.setAttribute("aria-label", t("typing"));
  if (on) { log.appendChild(typingRow); scrollDown(); } else typingRow.remove();
}

// Плашка дня над первым сообщением дня: «Сегодня», «Вчера», «27 октября» — словами
// браузера на языке гостя, своих текстов нет.
function dayKey(date) { return date.getFullYear() + "-" + date.getMonth() + "-" + date.getDate(); }
function dayLabel(date) {
  const today = new Date();
  const diff = Math.round((new Date(today.toDateString()) - new Date(date.toDateString())) / 864e5);
  if (diff === 0 || diff === 1) {
    const word = new Intl.RelativeTimeFormat(locale(), {numeric: "auto"}).format(-diff, "day");
    return word.charAt(0).toUpperCase() + word.slice(1);
  }
  return new Intl.DateTimeFormat(locale(), {day: "numeric", month: "long"}).format(date);
}
function clock(date) {
  return new Intl.DateTimeFormat(locale(), {hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(date);
}

function append(direction, text, at) {
  const date = at ? new Date(at) : new Date();
  if (dayKey(date) !== lastDay) {
    lastDay = dayKey(date);
    const chip = el("div", "day", dayLabel(date));
    chip.dataset.at = date.toISOString();
    log.appendChild(chip);
  }
  const div = el("div", "msg " + direction, text);
  div.appendChild(el("time", null, clock(date)));
  // Знак отеля у его ответа — как у собеседника (раскладка B, #746): копия кружка шапки,
  // буква или картинка отеля (spec 0066 §5); aria-hidden копируется вместе с ним.
  if (direction === "outbound") div.appendChild(Object.assign($("mono").cloneNode(true), {className: "ava", id: ""}));
  log.insertBefore(div, typingRow.isConnected ? typingRow : null);
  scrollDown();
  return div;
}

__UNREAD_JS__

async function api(path, options, from) {
  let response;
  try {
    response = await fetch((from || base) + path, Object.assign({headers: {"Content-Type": "application/json"}}, options));
  } catch (e) {
    // сети нет — как «не доставлено»
    return {ok: false, status: 0, data: null};
  }
  let data = null;
  try { data = await response.json(); } catch (e) { /* не-JSON — ниже */ }
  return {ok: response.ok, status: response.status, data};
}
// Чат не отвечает — 5xx или сети нет (#991): гостю ещё и куда идти. Цифру ресепшена
// страница взяла при загрузке, сейчас читать её неоткуда.
const down = (r) => !r.status || r.status >= 500;
const gateDown = () => [T.gate_error, ...LANGS.map((l) => T.call[l])].join("\n");
function failText(node) { node.textContent = t("not_delivered") + (node.dataset.down ? ". " + t("call") : ""); }

// Подпись для экранного чтения (и подсказка поля) по языку гостя: ключ — в `data-tl`, его
// перечитывает `applyLang`.
function label(node, key) {
  node.dataset.tl = key;
  node.setAttribute("aria-label", t(key));
  if ("placeholder" in node) node.placeholder = t(key);
}

// Оценка под итогом выполненной заявки (spec 0076 §5) — отдельным файлом с версией в адресе:
// грузится, когда в ленте впервые есть что оценить, и не утяжеляет страницу (#746). Не
// загрузился — кнопок нет, чат работает; следующее сообщение с оценкой попробует снова.
let fbLoad = null;
function feedback(bubble, state) {
  if (!state) return;
  fbLoad = fbLoad || new Promise((done) => {
    const script = el("script");
    script.src = "__FB_SRC__"; script.onload = done; script.onerror = () => { fbLoad = null; };
    document.head.appendChild(script);
  });
  fbLoad.then(() => fbWidget(bubble, state));
}

// Сообщение сервера — в ленту, если его там ещё нет: своё сообщение гостя опрос узнаёт
// по client_message_id, остальное — по id. Фото гостя (spec 0081) — картинкой.
function show(m) {
  if (shown.has(m.id) || shown.has(m.client_message_id)) return;
  shown.add(m.id);
  const div = append(m.direction, m.text, m.created_at);
  // Фото гостя (spec 0081): загрузчик картинки — только в странице, где фото включены.
  if (m.photo_url && typeof photo === "function") photo(div, m.photo_url);
  feedback(div, m.feedback);
  return div;
}

// Плашки над строкой ввода и плитка «Заказать в номер» в пустом чате.
const links = [...document.querySelectorAll("[data-path]")];
const ordersPill = $("orders-link");
// Скрепка формы реквизитов для АВР (spec 0049): подсказка уходит после первой отправки формы.
const clip = $("clip");
const clipHint = $("clip-hint");
const clipTexts = clip ? JSON.parse($("clip-texts").textContent) : null;
function documentsSent() {
  try { return localStorage.getItem("necturn:documents-sent") === "1"; } catch (e) { return false; }
}

// Пилюля языка: подпись текущего (ҚАЗ, РУС, ENG) — из кнопок её же меню.
function pill() { langBtn.textContent = langMenu.querySelector("[data-lang=" + lang + "]").textContent; }
function langOpen(on) { langMenu.hidden = !on; langBtn.setAttribute("aria-expanded", String(on)); }

function applyLang() {
  document.documentElement.lang = lang;
  $("room-line").textContent = roomLine(lang, room);
  input.placeholder = t("message");
  send.setAttribute("aria-label", t("send")); send.title = t("send");
  pill();
  langBtn.setAttribute("aria-label", t("language") + ": " + langBtn.textContent);
  for (const button of langMenu.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.lang === lang));
  }
  for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
  for (const node of document.querySelectorAll("[data-tl]")) label(node, node.dataset.tl);
  if (clip) {
    clip.setAttribute("aria-label", clipTexts.label[lang]); clip.title = clipTexts.label[lang];
    clipHint.textContent = clipTexts.hint[lang];
  }
  for (const chip of log.querySelectorAll(".day")) chip.textContent = dayLabel(new Date(chip.dataset.at));
  for (const fail of log.querySelectorAll(".fail")) failText(fail);
  typingRow.setAttribute("aria-label", t("typing"));
}
langBtn.addEventListener("click", () => langOpen(langMenu.hidden));
langMenu.addEventListener("click", (e) => {
  const button = e.target.closest("button");
  if (!button) return;
  lang = button.dataset.lang;
  try { sessionStorage.setItem(LANG_KEY, lang); } catch (e2) { /* без хранилища */ }
  applyLang(); langOpen(false); langBtn.focus();
});
document.addEventListener("click", (e) => { if (!e.target.closest("#langs")) langOpen(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") langOpen(false); });

// Пустой чат (в истории ни одного сообщения): крупное приветствие и плитки; плашка
// «Заказать в номер» над строкой ввода на это время уходит — та же кнопка есть плиткой.
function empty(on) {
  welcome.hidden = !on;
  if (ordersPill) ordersPill.hidden = on;
}
welcome.addEventListener("click", (e) => {
  const tile = e.target.closest("[data-fill]");
  if (tile) { input.value = t(tile.dataset.fill); input.focus(); }
});

// Строка комнаты в шапке до входа следует за полем комнаты (spec 0068 §2.2).
const roomField = $("room");
function gateRoomLine() {
  const typed = roomField.value.trim();
  $("room-line").textContent = typed ? LANGS.map((l) => roomLine(l, typed)).join(" · ") : "";
}
roomField.addEventListener("input", gateRoomLine);
roomField.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("code").focus(); } });

function toGate(message) {
  clearInterval(polling); polling = null;
  composer.hidden = true; gate.hidden = false; log.hidden = true; welcome.hidden = true;
  langOpen(false); langBtn.disabled = true; pill();
  gateRoomLine();
  const error = $("gate-error");
  error.hidden = !message;
  if (message) error.textContent = message;
}

function toChat() {
  gate.hidden = true; composer.hidden = false; log.hidden = false;
  langBtn.disabled = false;
  // «Заказать в номер» (spec 0038), «Забронировать столик» (spec 0064) и скрепка — только
  // после входа: без сессии их страницы всё равно вернут сюда же.
  for (const link of links) { link.href = base + "/" + link.dataset.path; link.hidden = false; }
  if (clip) { clip.href = base + "/documents"; clip.hidden = false; clipHint.hidden = documentsSent(); }
  const camera = $("photo-btn");
  if (camera) camera.hidden = false;
  applyLang();
  if (!polling) polling = setInterval(poll, 5000);
}

function greet() { append("outbound", t("greeting")); }

let pollBusy = false;
async function poll() {
  // Guard от наложения опросов: медленный ответ, пришедший после ответа следующего
  // тика, вернул бы курсор назад.
  if (pollBusy) return;
  pollBusy = true;
  try {
    const query = lastId ? "?after=" + lastId : "";
    const r = await api("/messages" + query, {method: "GET"});
    if (r.status === 401) { toGate(r.data && r.data.error && r.data.error.message); return; }
    // сеть мигнула — следующий тик догонит
    if (!r.ok || !r.data) return;
    for (const m of r.data.messages) {
      const div = show(m); if (div) noteIncoming(div, m.direction); lastId = m.id;
    }
    if (lastId) empty(false);
    if (!document.hidden) markSeen();
  } finally {
    pollBusy = false;
  }
}

const enter = $("enter");
enter.addEventListener("click", async (e) => {
  e.preventDefault();
  const typedRoom = roomField.value.trim();
  if (!typedRoom) { roomField.focus(); return; }
  const code = $("code").value.trim();
  if (!code) { $("code").focus(); return; }
  enter.disabled = true; enter.setAttribute("aria-busy", "true");
  let r;
  try {
    // Тройка тенант + комната + код — комната из поля (spec 0068 §2.2); сервер прежний.
    r = await api("/session", {method: "POST", body: JSON.stringify({code})}, roomBase(typedRoom));
  } finally {
    enter.disabled = false; enter.removeAttribute("aria-busy");
  }
  if (!r.ok) {
    const error = $("gate-error");
    error.textContent = down(r) ? gateDown() : (r.data && r.data.error && r.data.error.message) || T.gate_error;
    error.hidden = false;
    return;
  }
  // Вошёл в другую комнату — страница этой комнаты; там чат откроется по cookie сам.
  if (r.data && r.data.room_number !== room) { location.replace(roomBase(r.data.room_number)); return; }
  // Чат пуст, пока опрос не принесёт историю (повторный вход в тот же диалог) — тогда
  // приветствие с плитками уберёт сам опрос.
  log.replaceChildren(); lastId = null; lastDay = null; shown.clear(); retry = null;
  toChat(); empty(true); await poll();
  greet();
});

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;
  input.value = "";
  empty(false);
  // Повтор недоставленного: прежний ключ, а пузырь с пометкой уступает место новому внизу.
  let cid = crypto.randomUUID();
  if (retry && retry.text === text) { cid = retry.cid; retry.mine.nextSibling.remove(); retry.mine.remove(); }
  retry = null; shown.add(cid);
  const mine = append("inbound", text);
  send.disabled = true; typing(true);
  let r;
  try {
    r = await api("/messages", {method: "POST", body: JSON.stringify({text, client_message_id: cid})});
  } finally {
    send.disabled = false; typing(false);
  }
  if (r.status === 401) { toGate(r.data && r.data.error && r.data.error.message); return; }
  if (!r.ok) {
    // Не доставлено (в том числе обрыв сети): строка под самим сообщением, текст —
    // обратно в поле, чтобы отправить ещё раз без набора.
    mine.classList.add("failed");
    const fail = el("p", "fail");
    if (down(r)) fail.dataset.down = "1";
    failText(fail); mine.after(fail);
    retry = {text, cid, mine};
    if (!input.value) input.value = text;
    scrollDown();
    return;
  }
  r.data.replies.forEach((text, i) => show({id: r.data.reply_ids[i], direction: "outbound", text}));
  // Пауза лимита (#222): отказ в этом окне уже уходил, ход молчит — статус
  // над строкой ввода, иначе тишина неотличима от сломанного чата.
  const pause = $("chat-pause");
  pause.hidden = !r.data.pause;
  if (r.data.pause) pause.textContent = r.data.pause;
  // строка ввода выросла — последний ответ не должен уйти под неё
  scrollDown();
  const mark = newMark();
  if (mark) mark.remove();
  markSeen();
  // Курсор — не из ответа отправки (#117): прыжок на реплику хода терял исходящее подписчика,
  // записанное между входящим и репликой. Опрос сразу догоняет курсор и приносит такое
  // исходящее; сообщения этого хода он узнаёт по id.
  poll();
});

__PHOTO_LOADER__

// Старт: есть живая cookie → сразу чат с историей; иначе — экран кода. Пустая история
// (гость пришёл по QR с листка) — то же приветствие, что после ввода кода.
(async () => {
  const r = await api("/messages", {method: "GET"});
  // Сессия другой комнаты (переселили, иконка старой комнаты) — на адрес своей: в шапке
  // не должен стоять чужой номер (spec 0068 §2.2).
  if (r.ok && r.data && r.data.room_number !== room) { location.replace(roomBase(r.data.room_number)); return; }
  if (r.ok && r.data) {
    toChat();
    const divs = r.data.messages.map((m) => { lastId = m.id; return show(m); });
    if (!divs.length) { greet(); empty(true); }
    markHistory(r.data.messages, divs);
    if (!document.hidden) markSeen();
  } else {
    toGate(down(r) ? gateDown() : null);
  }
})();
