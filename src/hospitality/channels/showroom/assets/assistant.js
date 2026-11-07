/* Помощник 3D-витрины (spec 0069 §2.3, §5; вид — прототип #816): круглая кнопка в углу
 * модели и окно чата с подсказками-кнопками, в слоте [data-sr-slot=assistant].
 *
 * Фраза → POST cfg.navigateUrl (посетитель — /showroom/{slug}/navigate, кабинет —
 * /staff/{slug}/showroom/navigate) → {commands, reply, source}. Команды исполняет страница
 * через window.Showroom (showroom.js): focus_floor, focus_room, set_view, step_floor, reset,
 * find_rooms (номера уже подобрал сервер — applyRooms); черновик draft_request уходит
 * событием document "showroom:draft" — его карточку открывает форма запроса. Реплику пишет
 * модель: на страницу — только textContent. Переписка — только в памяти страницы, последние
 * пять реплик. Сеть, лимит или ошибка сервера — «Помощник отдыхает…», кнопки работают.
 * Свёрнутое окно — круглая кнопка; выбор посетителя страница помнит (#828, `isFolded`).
 */
import { h, icon, fill, isFolded, rememberFold } from "./showroom_core.js";

const LOG_SIZE = 5;

function start(S) {
  const slot = S.slot("assistant");
  if (!slot || slot.dataset.ready || !S.cfg.navigateUrl) return;
  slot.dataset.ready = "1";
  const { texts: t, viewer, model } = S;
  // Сторона, тип и номер в примерах — из сцены; нет ведущей стороны — нет и примеров с ней.
  const lead = model.leadSide ? model.sideNameLower(model.leadSide) : null;
  const placeholder = fill(lead ? t.chat_ph : t.chat_ph_plain, { side: lead, n: model.exampleRoom });

  const log = h("div", { class: "sr-chlog", "aria-live": "polite" });
  const input = h("input", { class: "sr-inp", type: "text", autocomplete: "off", enterkeyhint: "send", maxlength: "300", placeholder, "aria-label": placeholder });
  const send = h("button", { type: "submit", class: "sr-ibtn sr-chsend", "aria-label": t.chat_send }, [icon("send", 2.2)]);
  const form = h("form", { class: "sr-chform", novalidate: true }, [input, send]);
  const chips = [
    lead ? fill(t.chip_high, { side: lead }) : null,
    model.secondType ? fill(t.chip_type, { type: model.typeName(model.secondType) }) : null,
    t.chip_group,
    t.floor_up,
    model.exampleRoom ? fill(t.room_n, { n: model.exampleRoom }) : null,
  ].filter(Boolean);
  const sugs = h("div", { class: "sr-sugs" }, chips.map((text) => h("button", { type: "button", class: "sr-sug", text, onclick: () => ask(text) })));
  const close = h("button", { type: "button", class: "sr-ibtn", "aria-label": t.chat_close, title: t.chat_close, onclick: () => {
    setOpen(false, true);
    fab.focus();
  } }, [icon("down", 2.2)]);
  const chat = h("section", { class: "sr-chat", hidden: true, "aria-label": t.chat_title }, [
    h("div", { class: "sr-chhead" }, [h("h3", {}, [icon("chat"), h("span", { text: t.chat_title })]), close]),
    log, form, sugs, h("p", { class: "sr-chnote", text: t.assistant_hint }),
  ]);
  const bubble = icon("chat");
  const dots = icon("dots").firstChild;
  dots.setAttribute("stroke-width", "2.6");
  bubble.appendChild(dots);
  const fab = h("button", { type: "button", class: "sr-fab", "aria-expanded": "false", "aria-label": t.chat_open, title: t.chat_title }, [bubble]);
  slot.append(fab, chat);

  const isTouch = () => matchMedia("(hover: none)").matches || matchMedia("(pointer: coarse)").matches;
  let open = false;
  function setOpen(value, remember) {
    open = !!value;
    if (remember) rememberFold("chat", !open);
    chat.hidden = !open;
    fab.setAttribute("aria-expanded", String(open));
    fab.setAttribute("aria-label", open ? t.chat_close : t.chat_open);
    fit();
  }
  fab.addEventListener("click", () => {
    setOpen(!open, true);
    if (open && !isTouch()) input.focus();
  });
  // Телефон: окно прижато к низу экрана; открыта клавиатура — поднимаем окно над ней.
  function fit() {
    const vv = window.visualViewport;
    chat.style.bottom = "";
    chat.style.maxHeight = "";
    if (!vv || chat.hidden || getComputedStyle(chat).position !== "fixed") return;
    const keyboard = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    if (keyboard > 40) {
      chat.style.bottom = `${keyboard}px`;
      chat.style.maxHeight = `${Math.min(window.innerHeight * 0.33, vv.height - 8)}px`;
    }
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fit);
    window.visualViewport.addEventListener("scroll", fit);
  }

  const lines = [];
  function push(cls, text) {
    lines.push({ cls, text });
    while (lines.length > LOG_SIZE) lines.shift();
    log.replaceChildren(...lines.map((m) => h("p", { class: `sr-cm ${m.cls}`, text: m.text })));
  }

  function run(command) {
    const nav = S.nav || {};
    switch (command.type) {
      case "focus_floor":
        if (nav.goFloor) nav.goFloor(command.floor);
        break;
      case "focus_room":
        if (nav.goRoom) nav.goRoom(String(command.room));
        break;
      case "set_view":
        if (nav.applyView) nav.applyView(command.preset);
        break;
      case "step_floor": {
        const current = S.currentFloor();
        if (current !== null && nav.goFloor) nav.goFloor(current + (command.direction === "up" ? 1 : -1));
        break;
      }
      case "reset":
        if (viewer) viewer.reset();
        S.setRooms(S.audience(), [], { fly: false });
        S.set({ selRoom: null, selFloor: null, found: [], activeView: "home" });
        break;
      case "find_rooms":
        S.applyRooms(command.rooms || []);
        break;
      case "draft_request":
        document.dispatchEvent(new CustomEvent("showroom:draft", { detail: command }));
        break;
    }
  }

  async function call(text) {
    try {
      const response = await fetch(S.cfg.navigateUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ text, language: S.lang, audience: S.audience() }),
      });
      return response.ok ? await response.json() : null;
    } catch (error) {
      return null;
    }
  }

  let busy = false;
  async function ask(raw) {
    const text = String(raw || "").trim().slice(0, 300);
    if (!text || busy) return;
    busy = true;
    send.disabled = true;
    push("me", text);
    push("bot wait", t.chat_wait);
    const result = await call(text);
    lines.pop();
    busy = false;
    send.disabled = false;
    if (!result) {
      push("bot", t.assistant_resting);
      return;
    }
    (result.commands || []).forEach(run);
    push("bot", result.reply || t.assistant_resting);
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value;
    input.value = "";
    ask(text);
  });
  input.addEventListener("focus", () => setTimeout(fit, 250));
  input.addEventListener("blur", () => setTimeout(fit, 250));

  // Широкий экран с мышью — окно открыто сразу, как в прототипе; свернул посетитель — свёрнуто.
  const stage = slot.closest(".sr-stage");
  setOpen(!isFolded("chat", !(!!stage && stage.clientWidth >= 900 && !isTouch())));
}

if (window.Showroom) start(window.Showroom);
else document.addEventListener("showroom:ready", (event) => start(event.detail), { once: true });
