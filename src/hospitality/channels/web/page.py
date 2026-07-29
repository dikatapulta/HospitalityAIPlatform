"""Страница веб-чата — один статический HTML (spec 0027 §3.4, P-1, R-3).

Инлайн CSS/JS, без сборки и зависимостей: Next.js закреплён за кабинетом
персонала (ADR-002), для гостевой страницы это была бы вторая сборка и второй
деплой. Тексты интерфейса — en+ru (демография: ~70% иностранцев; язык ответов
AI — язык гостя). Slug и комнату страница читает из своего URL; сессия — в
HttpOnly-cookie, JS токена не видит.

Экран входа = экран согласия: кнопка «войти» и есть согласие на обработку ПД
(spec 0029). Текст — общий канон каналов (`channels/common/consent.py`, дословная
копия `docs/legal/consent-text.md`), во всех трёх языках: до входа язык гостя
неизвестен всегда. Подстановка — через маркеры `__…__`, а не `str.format`:
в HTML полно фигурных скобок (CSS, JS).
"""

from __future__ import annotations

import html

from hospitality.channels.common.consent import (
    CONSENT_VERSION,
    consent_button_label,
    consent_text,
)
from hospitality.platform.legal import privacy_policy_url

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hotel chat</title>
<style>
  :root { --bg:#f4f5f7; --card:#fff; --accent:#1a73e8; --muted:#667; }
  * { box-sizing:border-box; margin:0; }
  body { font:16px/1.45 system-ui,sans-serif; background:var(--bg); height:100dvh;
         display:flex; flex-direction:column; }
  header { padding:12px 16px; background:var(--card); border-bottom:1px solid #e3e5e8; }
  header h1 { font-size:17px; }
  header p { font-size:13px; color:var(--muted); }
  main { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:8px; }
  .msg { max-width:85%; padding:9px 12px; border-radius:12px; white-space:pre-wrap;
         overflow-wrap:break-word; }
  .inbound { align-self:flex-end; background:var(--accent); color:#fff; }
  .outbound { align-self:flex-start; background:var(--card); border:1px solid #e3e5e8; }
  form, .gate { padding:12px 16px; background:var(--card); border-top:1px solid #e3e5e8; }
  .row { display:flex; gap:8px; }
  input { flex:1; padding:10px 12px; font-size:16px; border:1px solid #c9ccd1;
          border-radius:8px; }
  button { padding:10px 16px; font-size:15px; border:0; border-radius:8px;
           background:var(--accent); color:#fff; }
  button:disabled { opacity:.5; }
  .consent { max-height:170px; overflow-y:auto; margin-top:10px; padding:10px 12px;
             border:1px solid #d6d8dc; border-radius:8px; background:#fafbfc;
             font-size:12.5px; line-height:1.5; color:#444; white-space:pre-wrap; }
  .consent a { color:var(--accent); }
  .wide { width:100%; margin-top:8px; }
  .hint { font-size:12.5px; color:var(--muted); margin-top:8px; }
  .error { color:#b3261e; font-size:14px; margin-top:8px; white-space:pre-wrap; }
  .hidden { display:none; }
</style>
</head>
<body>
<header>
  <h1>Hotel guest chat · Чат с отелем</h1>
  <p id="room-line"></p>
</header>
<main id="log"></main>

<div class="gate" id="gate">
  <p>Тіркелу карточкасындағы кодты енгізіңіз.<br>
     Введите код заселения с карточки, выданной на ресепшене.<br>
     Enter the check-in code from your reception card.</p>
  <div class="row" style="margin-top:8px">
    <input id="code" autocomplete="one-time-code" placeholder="K7M-9QT" maxlength="12">
  </div>
  <div class="consent">__CONSENT_TEXT__</div>
  <button id="enter" class="wide">__CONSENT_BUTTON__</button>
  <p class="hint">__CONSENT_VERSION__</p>
  <p class="error hidden" id="gate-error"></p>
</div>

<form class="hidden" id="composer">
  <div class="row">
    <input id="text" autocomplete="off" placeholder="Message · Сообщение">
    <button id="send">➤</button>
  </div>
  <p class="error hidden" id="chat-error"></p>
</form>

<script>
"use strict";
const base = location.pathname.replace(/\\/$/, "");
const room = decodeURIComponent(base.split("/").pop());
document.getElementById("room-line").textContent = "Room · Комната " + room;
const log = document.getElementById("log");
const gate = document.getElementById("gate");
const composer = document.getElementById("composer");
let lastId = null;
let polling = null;

function show(el, on) { el.classList.toggle("hidden", !on); }
function say(el, text) { el.textContent = text; show(el, true); }
function append(direction, text) {
  const div = document.createElement("div");
  div.className = "msg " + direction;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function api(path, options) {
  const response = await fetch(base + path, Object.assign({
    headers: {"Content-Type": "application/json"}}, options));
  let data = null;
  try { data = await response.json(); } catch (e) { /* не-JSON — ниже */ }
  return {ok: response.ok, status: response.status, data};
}

function toGate(message) {
  clearInterval(polling); polling = null;
  show(composer, false); show(gate, true);
  if (message) say(document.getElementById("gate-error"), message);
}

function toChat() {
  show(gate, false); show(composer, true);
  if (!polling) polling = setInterval(poll, 5000);
}

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
    if (!r.ok || !r.data) return;  // сеть мигнула — следующий тик догонит
    for (const m of r.data.messages) { append(m.direction, m.text); lastId = m.id; }
  } finally {
    pollBusy = false;
  }
}

document.getElementById("enter").addEventListener("click", async (e) => {
  e.preventDefault();
  const code = document.getElementById("code").value.trim();
  if (!code) return;
  const r = await api("/session", {method: "POST", body: JSON.stringify({code})});
  if (!r.ok) {
    const detail = r.data && r.data.error && r.data.error.message;
    say(document.getElementById("gate-error"),
        detail || "Error. Try again · Ошибка, попробуйте ещё раз");
    return;
  }
  log.replaceChildren(); lastId = null;
  toChat(); await poll();
  append("outbound", "You're connected. How can we help?\\n" +
                     "Вы подключены. Чем можем помочь?");
});

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("text");
  const text = input.value.trim();
  if (!text) return;
  input.value = ""; append("inbound", text);
  const body = {text, client_message_id: crypto.randomUUID()};
  // На время отправки блокируем poll (ревью PR #116): опрос со старым курсором,
  // ушедший параллельно, нарисовал бы реплику хода за миг до ответа POST.
  pollBusy = true;
  let r;
  try {
    r = await api("/messages", {method: "POST", body: JSON.stringify(body)});
  } finally {
    pollBusy = false;
  }
  if (r.status === 401) { toGate(r.data && r.data.error && r.data.error.message); return; }
  if (!r.ok) { say(document.getElementById("chat-error"),
    "Not delivered, try again · Не доставлено, попробуйте ещё раз"); return; }
  show(document.getElementById("chat-error"), false);
  for (const reply of r.data.replies) append("outbound", reply);
  // Сообщения этого хода уже на экране — сдвигаем курсор poll'а, иначе
  // следующий опрос принесёт их из истории второй раз (дубли, живой баг 27.07).
  if (r.data.last_message_id) lastId = r.data.last_message_id;
});

// Старт: есть живая cookie → сразу чат с историей; иначе — экран кода.
(async () => {
  const r = await api("/messages", {method: "GET"});
  if (r.ok && r.data) {
    toChat();
    for (const m of r.data.messages) { append(m.direction, m.text); lastId = m.id; }
  } else {
    toGate(null);
  }
})();
</script>
</body>
</html>
"""


def render() -> str:
    """HTML страницы; функция — точка будущей параметризации (язык, бренд).

    Текст согласия подставляется здесь, а не хранится в шаблоне: источник —
    общий канон каналов, и расходиться копиям нельзя (spec 0029 §2).
    """
    url = html.escape(privacy_policy_url())
    link = f'<a href="{url}" target="_blank" rel="noopener">{url}</a>'
    body = html.escape(consent_text(None)).replace(url, link)
    return (
        _PAGE.replace("__CONSENT_TEXT__", body)
        .replace("__CONSENT_BUTTON__", html.escape(consent_button_label(None)))
        .replace("__CONSENT_VERSION__", f"Согласие · Consent {CONSENT_VERSION}")
    )
