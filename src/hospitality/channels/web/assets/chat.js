"use strict";
// Страница веб-чата (spec 0027 §3.4, вид — #746). Вклеивается в page.py при импорте;
// маркер ниже заменяется скриптом непрочитанного (assets/unread.js, #223).
const base = location.pathname.replace(/\/$/, "");
const room = decodeURIComponent(base.split("/").pop());
const $ = (id) => document.getElementById(id);
// Лента прокручивается вместе со страницей (.app) под плавающими шапкой и строкой ввода.
const app = document.querySelector(".app");
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
let lastId = null;
let polling = null;
let lastDay = null;

// Язык надписей после входа: выбор гостя в шапке, иначе язык телефона. Экран входа —
// на трёх языках всегда: до входа язык гостя неизвестен (spec 0029).
let lang = (() => {
  try { const saved = sessionStorage.getItem(LANG_KEY); if (LANGS.includes(saved)) return saved; } catch (e) { /* без хранилища */ }
  const code = (navigator.language || "").toLowerCase().split("-")[0];
  return LANGS.includes(code) ? code : "en";
})();
const t = (key) => T[key][lang];
const roomLine = (l) => T.room[l].replace("{}", room);
const locale = () => (lang === "kk" ? "kk-KZ" : lang);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}
function scrollDown() { app.scrollTop = app.scrollHeight; }

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

async function api(path, options) {
  let response;
  try {
    response = await fetch(base + path, Object.assign({headers: {"Content-Type": "application/json"}}, options));
  } catch (e) {
    // сети нет — как «не доставлено»
    return {ok: false, status: 0, data: null};
  }
  let data = null;
  try { data = await response.json(); } catch (e) { /* не-JSON — ниже */ }
  return {ok: response.ok, status: response.status, data};
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
  $("room-line").textContent = roomLine(lang);
  input.placeholder = t("message");
  send.setAttribute("aria-label", t("send")); send.title = t("send");
  pill();
  langBtn.setAttribute("aria-label", t("language") + ": " + langBtn.textContent);
  for (const button of langMenu.querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.dataset.lang === lang));
  }
  for (const node of document.querySelectorAll("[data-t]")) node.textContent = t(node.dataset.t);
  if (clip) {
    clip.setAttribute("aria-label", clipTexts.label[lang]); clip.title = clipTexts.label[lang];
    clipHint.textContent = clipTexts.hint[lang];
  }
  for (const chip of log.querySelectorAll(".day")) chip.textContent = dayLabel(new Date(chip.dataset.at));
  for (const fail of log.querySelectorAll(".fail")) fail.textContent = t("not_delivered");
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

function toGate(message) {
  clearInterval(polling); polling = null;
  composer.hidden = true; gate.hidden = false; log.hidden = true; welcome.hidden = true;
  langOpen(false); langBtn.disabled = true; pill();
  $("room-line").textContent = LANGS.map(roomLine).join(" · ");
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
  applyLang();
  if (!polling) polling = setInterval(poll, 5000);
}

function greet() { append("outbound", t("greeting")); }

let pollBusy = false;
async function poll() {
  // Guard от наложения опросов: медленный ответ + следующий тик интервала
  // читали бы историю с одним и тем же курсором и рисовали её дважды.
  if (pollBusy) return;
  pollBusy = true;
  try {
    const query = lastId ? "?after=" + lastId : "";
    const r = await api("/messages" + query, {method: "GET"});
    if (r.status === 401) { toGate(r.data && r.data.error && r.data.error.message); return; }
    // сеть мигнула — следующий тик догонит
    if (!r.ok || !r.data) return;
    for (const m of r.data.messages) {
      noteIncoming(append(m.direction, m.text, m.created_at), m.direction); lastId = m.id;
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
  const code = $("code").value.trim();
  if (!code) { $("code").focus(); return; }
  enter.disabled = true; enter.setAttribute("aria-busy", "true");
  let r;
  try {
    r = await api("/session", {method: "POST", body: JSON.stringify({code})});
  } finally {
    enter.disabled = false; enter.removeAttribute("aria-busy");
  }
  if (!r.ok) {
    const error = $("gate-error");
    error.textContent = (r.data && r.data.error && r.data.error.message) || T.gate_error;
    error.hidden = false;
    return;
  }
  // Чат пуст, пока опрос не принесёт историю (повторный вход в тот же диалог) — тогда
  // приветствие с плитками уберёт сам опрос.
  log.replaceChildren(); lastId = null; lastDay = null;
  toChat(); empty(true); await poll();
  greet();
});

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;
  input.value = "";
  empty(false);
  const mine = append("inbound", text);
  const body = {text, client_message_id: crypto.randomUUID()};
  // На время отправки блокируем poll (ревью PR #116): опрос со старым курсором,
  // ушедший параллельно, нарисовал бы реплику хода за миг до ответа POST.
  pollBusy = true; send.disabled = true; typing(true);
  let r;
  try {
    r = await api("/messages", {method: "POST", body: JSON.stringify(body)});
  } finally {
    pollBusy = false; send.disabled = false; typing(false);
  }
  if (r.status === 401) { toGate(r.data && r.data.error && r.data.error.message); return; }
  if (!r.ok) {
    // Не доставлено (в том числе обрыв сети): строка под самим сообщением, текст —
    // обратно в поле, чтобы отправить ещё раз без набора.
    mine.classList.add("failed");
    mine.after(el("p", "fail", t("not_delivered")));
    if (!input.value) input.value = text;
    scrollDown();
    return;
  }
  for (const reply of r.data.replies) append("outbound", reply);
  // Пауза лимита (#222): отказ в этом окне уже уходил, ход молчит — статус
  // над строкой ввода, иначе тишина неотличима от сломанного чата.
  const pause = $("chat-pause");
  pause.hidden = !r.data.pause;
  if (r.data.pause) pause.textContent = r.data.pause;
  // строка ввода выросла — последний ответ не должен уйти под неё
  scrollDown();
  // Сообщения этого хода уже на экране — сдвигаем курсор poll'а, иначе
  // следующий опрос принесёт их из истории второй раз (дубли, живой баг 27.07).
  if (r.data.last_message_id) lastId = r.data.last_message_id;
  const mark = newMark();
  if (mark) mark.remove();
  markSeen();
});

// Старт: есть живая cookie → сразу чат с историей; иначе — экран кода. Пустая история
// (гость пришёл по QR с листка) — то же приветствие, что после ввода кода.
(async () => {
  const r = await api("/messages", {method: "GET"});
  if (r.ok && r.data) {
    toChat();
    const divs = r.data.messages.map((m) => {
      lastId = m.id; return append(m.direction, m.text, m.created_at);
    });
    if (!divs.length) { greet(); empty(true); }
    markHistory(r.data.messages, divs);
    if (!document.hidden) markSeen();
  } else {
    toGate(null);
  }
})();
