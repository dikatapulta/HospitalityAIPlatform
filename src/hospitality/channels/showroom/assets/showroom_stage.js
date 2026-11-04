/* Всё поверх модели 3D-витрины (#816, часть Б): «Назад», «где я», поиск номера (F), «?» с
 * жестами, линейка этажей справа, ракурсы, подсказка, карточка номера и полосы «Вид из окна»
 * и «Схема номера». Клавиши для показа: 1–4 — ракурсы, F — поиск, Esc — назад, ? — помощь.
 * Методы просмотрщика, которых может не быть (openWindow, openRoom, leave, setWindowFloor,
 * hoverFloor), страница проверяет — нет метода, нет и кнопки.
 */
import { h, icon, fill, plural } from "./showroom_core.js";

const VIEWS = [["mountains", "view_mountains"], ["city", "view_city"], ["top", "view_top"], ["home", "view_home"]];

export function createStage(stage, S) {
  const { texts: t, model, lang, state, viewer, can } = S;
  const floorN = (n) => fill(t.floor_n, { n });
  const roomN = (n) => fill(t.room_n, { n });

  const hint = h("p", { class: "sr-hintbar" });
  const tip = h("div", { class: "sr-tip", hidden: true });
  const wframe = h("div", { class: "sr-wframe", hidden: true });
  const card = h("section", { class: "sr-card", hidden: true, "aria-live": "polite" });
  const ibar = h("section", { class: "sr-ibar", hidden: true });
  const back = h("button", { type: "button", class: "sr-back", "aria-disabled": "true", onclick: () => back.getAttribute("aria-disabled") !== "true" && goBack() }, [icon("back", 2.4), h("span", { text: t.nav_back })]);
  const crumbs = h("nav", { class: "sr-crumbs", "aria-label": t.crumbs_aria });
  const findInput = h("input", { type: "text", inputmode: "search", autocomplete: "off", maxlength: "10", enterkeyhint: "search", placeholder: fill(t.find_ph, { n: model.exampleRoom }), "aria-label": t.find_btn });
  const find = h("form", { class: "sr-find sr-glass", role: "search", novalidate: true }, [
    findInput,
    h("kbd", { class: "sr-fk", "aria-hidden": "true", text: "F" }),
    h("button", { type: "submit", class: "sr-ibtn", "aria-label": t.find_btn, title: `${t.find_btn} (F)` }, [icon("search", 2.2)]),
    h("button", { type: "button", class: "sr-ibtn sr-fx", "aria-label": t.find_close, text: "×", onclick: () => closeFind() }),
  ]);
  const help = h("button", { type: "button", class: "sr-ibtn sr-helpb", "aria-expanded": "false", "aria-label": t.help_btn, title: t.help_btn, text: "?", onclick: () => (coachMode === "help" ? hideCoach() : showCoach("help")) });
  const nav = h("div", { class: "sr-nav" }, [back, crumbs, h("span", { class: "sr-navsp" }), find, help]);
  const findMsg = h("p", { class: "sr-findmsg", role: "status", hidden: true });
  const rail = h("div", { class: "sr-rail sr-glass", role: "group", "aria-label": t.rail_aria });
  const rtip = h("div", { class: "sr-rtip", hidden: true });
  const views = h("div", { class: "sr-views sr-glass", role: "group", "aria-label": t.views_aria });
  const coach = h("div", { class: "sr-coach", hidden: true });
  const assistantSlot = h("div", { class: "sr-assistant-slot", "data-sr-slot": "assistant" });
  stage.append(wframe, hint, tip, card, ibar, nav, findMsg, rail, rtip, views, assistantSlot, coach);
  // replaceChildren(null) вставил бы текст «null» — пустые части отбрасываем.
  const put = (node, ...kids) => node.replaceChildren(...kids.filter(Boolean));

  // ---------- Переходы ----------
  // focusFloor, focusRoom и setView из окна или номера просмотрщик сам сначала возвращает к
  // модели (договор PR #818); во время перелёта они отвечают false — тогда состояние не трогаем.
  function leaveImmersive() {
    if (state.mode !== "model" && can("backToBuilding")) viewer.backToBuilding();
  }
  function applyView(name) {
    if (viewer && viewer.setView(name) !== false) S.set({ activeView: name, selRoom: null, selFloor: null, hintSeen: true });
  }
  function goFloor(f) {
    if (viewer && viewer.focusFloor(f) !== false) S.set({ selRoom: null, selFloor: f, activeView: null });
  }
  function collapse() {
    if (viewer && viewer.focusFloor(null) !== false) S.set({ selFloor: null, selRoom: null, activeView: "home" });
  }
  function goRoom(number) {
    if (viewer) viewer.focusRoom(number);
  }
  function goBack() {
    if (coachMode === "help") return hideCoach();
    if (state.reqOpen) return S.closeRequest();
    if (state.mode !== "model") return leaveImmersive();
    if (state.selRoom) return goFloor(model.rooms.get(state.selRoom).floor);
    if (state.selFloor !== null) collapse();
  }

  // ---------- «Где я» ----------
  function levels() {
    const list = [{ k: "home", label: t.crumb_home }];
    let floor = null;
    let room = null;
    let leaf = null;
    if (state.mode !== "model" && state.win) {
      room = state.win.room;
      floor = state.win.floor;
      leaf = state.mode === "room" ? t.crumb_inside : t.win_view;
    } else {
      room = state.selRoom;
      floor = room ? model.rooms.get(room).floor : state.selFloor;
    }
    if (floor !== null && floor !== undefined) list.push({ k: "floor", f: floor, label: floorN(floor) });
    if (room) list.push({ k: "room", label: String(room) });
    if (leaf) list.push({ k: "leaf", label: leaf });
    return list;
  }
  function renderCrumbs(list) {
    const narrow = stage.classList.contains("narrow");
    const ol = h("ol", { class: "sr-glass" });
    list.forEach((c, i) => {
      if (i) ol.appendChild(h("li", { class: "sep", "aria-hidden": "true", text: "›" }));
      if (i === list.length - 1) {
        ol.appendChild(h("li", {}, [h("span", { "aria-current": "location", text: c.label })]));
        return;
      }
      const iconOnly = i === 0 && narrow && list.length > 1;
      const button = h("button", { type: "button", "aria-label": iconOnly ? c.label : null, title: iconOnly ? c.label : null, onclick: () => {
        if (c.k === "home") applyView("home");
        else if (c.k === "floor") goFloor(c.f);
        else if (c.k === "room") leaveImmersive();
      } }, [iconOnly ? icon("building") : document.createTextNode(c.label)]);
      ol.appendChild(h("li", {}, [button]));
    });
    crumbs.replaceChildren(ol);
    ol.scrollLeft = ol.scrollWidth;
    ol.classList.toggle("cut", ol.scrollLeft > 0);
    back.setAttribute("aria-disabled", String(!(list.length > 1 || state.reqOpen || coachMode === "help")));
  }

  // ---------- Линейка этажей ----------
  function railLabel(f) {
    if (model.isSale(f)) {
      const rooms = model.roomsOf(f);
      const inGroup = rooms.filter((r) => state.group.includes(r.number)).length;
      const text = `${floorN(f)} · ${fill(t.floor_rooms, { n: rooms.length, rooms: plural(lang, rooms.length, t.stat_rooms) })}`;
      return inGroup ? `${text} · ${fill(t.floor_in_group, { n: inGroup })}` : text;
    }
    const label = model.floorLabel(f);
    return `${floorN(f)} · ${label ? label.charAt(0).toLowerCase() + label.slice(1) : t.rail_no_sale}`;
  }
  const railButtons = [];
  for (let f = S.scene.building.floors_total; f >= 1; f -= 1) {
    const sale = model.isSale(f);
    const b = h("button", { type: "button", class: sale ? "sr-rf" : "sr-rf ns", "data-rf": String(f), "aria-pressed": sale ? "false" : null, "aria-disabled": sale ? null : "true", text: String(f) });
    b.addEventListener("click", (e) => {
      if (e.pointerType !== "mouse") showRailTip(b, true);
      if (!sale) return;
      if (state.mode === "model" && state.selFloor === f && !state.selRoom) collapse();
      else goFloor(f);
    });
    b.addEventListener("pointerover", (e) => {
      if (e.pointerType !== "mouse") return;
      showRailTip(b, false);
      if (can("hoverFloor") && state.mode === "model") viewer.hoverFloor(f);
    });
    b.addEventListener("focus", () => b.matches(":focus-visible") && showRailTip(b, false));
    b.addEventListener("blur", hideRailTip);
    railButtons.push(b);
  }
  rail.append(...railButtons);
  rail.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "mouse") return;
    hideRailTip();
    if (can("hoverFloor")) viewer.hoverFloor(null);
  });
  rail.addEventListener("scroll", hideRailTip, { passive: true });
  let rtipTimer = null;
  function showRailTip(b, auto) {
    const sr = stage.getBoundingClientRect();
    const br = b.getBoundingClientRect();
    rtip.textContent = railLabel(Number(b.dataset.rf));
    rtip.style.top = `${br.top + br.height / 2 - sr.top}px`;
    rtip.hidden = false;
    clearTimeout(rtipTimer);
    if (auto) rtipTimer = setTimeout(hideRailTip, 1800);
  }
  function hideRailTip() {
    clearTimeout(rtipTimer);
    rtip.hidden = true;
  }
  function renderRail(list) {
    const level = list.find((c) => c.k === "floor");
    const current = level ? level.f : null;
    const groupFloors = new Set(state.group.map((n) => model.rooms.get(n).floor));
    for (const b of railButtons) {
      const f = Number(b.dataset.rf);
      const on = state.mode === "model" && state.selFloor === f;
      if (model.isSale(f)) b.setAttribute("aria-pressed", String(on));
      b.classList.toggle("cur", f === current && !on);
      b.classList.toggle("g", state.audience === "group" && groupFloors.has(f));
      b.setAttribute("aria-label", railLabel(f));
      if ((on || f === current) && rail.scrollHeight > rail.clientHeight + 1) {
        const top = b.offsetTop;
        if (top < rail.scrollTop || top + b.offsetHeight > rail.scrollTop + rail.clientHeight) rail.scrollTop = top - (rail.clientHeight - b.offsetHeight) / 2;
      }
    }
  }

  // ---------- Ракурсы ----------
  const viewButtons = VIEWS.map(([name, key], i) =>
    h("button", { type: "button", class: "sr-vb", "data-view": name, "aria-pressed": "false", title: `${name === "home" ? t.home_title : t[key]} (${i + 1})`, onclick: () => applyView(name) }, [icon(name), h("span", { text: t[key] })])
  );
  views.append(...viewButtons);

  // ---------- Поиск номера ----------
  let findTimer = null;
  function openFind() {
    if (stage.classList.contains("narrow")) find.classList.add("open");
    findInput.focus();
    findInput.select();
  }
  function closeFind() {
    find.classList.remove("open");
    findInput.blur();
    findMsg.hidden = true;
  }
  find.addEventListener("submit", (e) => {
    e.preventDefault();
    if (stage.classList.contains("narrow") && !find.classList.contains("open")) return openFind();
    const typed = findInput.value.trim();
    if (!typed) return openFind();
    const number = model.rooms.has(typed) ? typed : [...model.rooms.keys()].find((n) => n.toLowerCase() === typed.toLowerCase());
    if (!number || !viewer) {
      findMsg.textContent = t.room_not_found;
      findMsg.hidden = false;
      clearTimeout(findTimer);
      findTimer = setTimeout(() => (findMsg.hidden = true), 3500);
      return;
    }
    closeFind();
    goRoom(number);
  });
  findInput.addEventListener("input", () => (findMsg.hidden = true));

  // ---------- Подсказка жестов ----------
  let coachMode = null;
  let coachTimer = null;
  const isTouch = () => matchMedia("(hover: none)").matches || matchMedia("(pointer: coarse)").matches;
  function showCoach(mode) {
    coachMode = mode;
    clearTimeout(coachTimer);
    const helpMode = mode === "help";
    const rows = isTouch() ? [["rotate", t.coach_touch_rotate], ["zoom", t.coach_touch_zoom]] : [["rotate", t.coach_mouse_rotate], ["zoom", t.coach_mouse_zoom], ["pan", t.coach_mouse_pan]];
    const keys = helpMode && !isTouch() ? [["1", t.view_mountains], ["2", t.view_city], ["3", t.view_top], ["4", t.view_home], ["F", t.find_btn], ["Esc", t.nav_back]] : null;
    coach.className = helpMode ? "sr-coach" : "sr-coach auto";
    coach.setAttribute("role", helpMode ? "dialog" : "status");
    coach.setAttribute("aria-label", t.coach_title);
    put(coach,
      h("div", { class: "sr-chd" }, [h("p", { class: "sr-ceyebrow gold", text: t.coach_title }), helpMode ? h("button", { type: "button", class: "sr-x", "aria-label": t.close, text: "×", onclick: () => hideCoach() }) : null]),
      h("ul", { class: "sr-gest" }, rows.map(([name, pair]) => {
        const [what, does] = String(pair).split("|");
        return h("li", {}, [icon(name), h("span", {}, [h("b", { text: what }), ` — ${does || ""}`])]);
      })),
      keys ? h("p", { class: "sr-ceyebrow", text: t.coach_keys }) : null,
      keys ? h("ul", { class: "sr-hkeys" }, keys.map(([k, v]) => h("li", {}, [h("kbd", { text: k }), h("span", { text: v })]))) : null,
      helpMode ? null : h("i", { class: "sr-cbar" })
    );
    coach.hidden = false;
    help.setAttribute("aria-expanded", String(helpMode));
    if (!helpMode) coachTimer = setTimeout(hideCoach, 5000);
    render();
  }
  function hideCoach() {
    if (!coachMode) return;
    coachMode = null;
    clearTimeout(coachTimer);
    coach.hidden = true;
    help.setAttribute("aria-expanded", "false");
    render();
  }
  stage.addEventListener("pointerdown", (e) => {
    if (coachMode === "auto") hideCoach();
    else if (coachMode === "help" && !e.target.closest(".sr-coach, .sr-helpb")) hideCoach();
    if (find.classList.contains("open") && !e.target.closest(".sr-find")) closeFind();
    if (!state.hintSeen && e.target.tagName === "CANVAS") S.set({ hintSeen: true });
  }, true);

  // Клавиши для показа. Пока курсор в поле ввода, клавиши не перехватываем.
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.altKey || e.ctrlKey || e.metaKey) return;
    const el = document.activeElement;
    const typing = !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
    if (coachMode === "auto" && e.key !== "Shift") hideCoach();
    if (e.key === "Escape") {
      e.preventDefault();
      if (el === findInput) return closeFind();
      if (typing) return el.blur();
      return goBack();
    }
    if (typing) return;
    if (/^[1-4]$/.test(e.key)) {
      e.preventDefault();
      applyView(VIEWS[Number(e.key) - 1][0]);
    } else if (e.code === "KeyF") {
      e.preventDefault();
      openFind();
    } else if (e.key === "?") {
      e.preventDefault();
      if (coachMode === "help") hideCoach();
      else showCoach("help");
    }
  });

  // ---------- Карточка номера и полосы режимов ----------
  function roomCard(number) {
    const room = model.rooms.get(number);
    const type = S.scene.room_types[room.type_key] || {};
    const audience = state.audience;
    const chosen = S.rooms(audience).includes(number);
    const canToggle = state.tab !== "floors" && (audience === "group" || S.hasRequests);
    const typeText = model.typeName(room.type_key) + (type.note ? ` · ${model.textFor(type.note)}` : "");
    put(card,
      h("div", { class: "sr-chead" }, [
        h("div", {}, [h("p", { class: "sr-ceyebrow", text: `${floorN(room.floor)} · ${typeText}` }), h("h3", { text: roomN(number) })]),
        h("button", { type: "button", class: "sr-x", "aria-label": t.close, text: "×", onclick: () => viewer && viewer.select(null) }),
      ]),
      h("div", { class: "sr-pills" }, [
        h("span", { class: "sr-pill" }, [h("i", { class: room.lead ? "sr-dot m" : "sr-dot c" }), model.sideName(room.side)]),
        chosen ? h("span", { class: "sr-pill gold", text: audience === "group" ? t.key_group : t.key_booking }) : null,
      ]),
      h("p", { class: "sr-price", text: type.price_note ? fill(t.price_note, { price: model.textFor(type.price_note) }) : t.no_price }),
      can("openWindow") || can("openRoom") ? h("div", { class: "sr-cbtns" }, [
        can("openWindow") ? h("button", { type: "button", class: "sr-btn primary", text: t.win_view, onclick: () => viewer.openWindow(number) }) : null,
        can("openRoom") ? h("button", { type: "button", class: "sr-btn", text: t.look_in, onclick: () => viewer.openRoom(number) }) : null,
      ]) : null,
      canToggle ? h("button", { type: "button", class: "sr-btn ghost", onclick: () => S.toggleRoom(audience, number),
        text: audience === "group" ? (chosen ? t.remove_group : t.add_group) : (chosen ? t.remove_booking : t.add_booking) }) : null
    );
  }
  // Полоса «Вид из окна» строится раз на вход в окно; ползунок этажа меняет только надписи —
  // перестройка посреди перетаскивания оборвала бы жест.
  const winBar = { title: null, sub: null, out: null, slider: null, look: null };
  function updateWindowBar() {
    const win = state.win;
    const room = win && win.room ? model.rooms.get(win.room) : null;
    const side = room ? model.sideName(room.side) : "";
    winBar.title.textContent = fill(t.win_title, { n: win.floor, side });
    winBar.sub.textContent = room ? fill(t.win_sub, { n: room.number, type: model.typeName(room.type_key) }) : t.win_no_room;
    winBar.out.textContent = String(win.floor);
    if (document.activeElement !== winBar.slider) winBar.slider.value = String(Math.max(0, model.saleFloors.indexOf(win.floor)));
    if (winBar.look) winBar.look.disabled = !room;
  }
  function renderIbar() {
    const win = state.win;
    const room = win && win.room ? model.rooms.get(win.room) : null;
    const sideText = room ? model.sideName(room.side) : "";
    const leave = h("button", { type: "button", class: "sr-btn primary", text: t.back_building, onclick: () => leaveImmersive() });
    if (state.mode === "window" && win) {
      const floors = model.saleFloors;
      winBar.slider = h("input", { type: "range", min: "0", max: String(Math.max(0, floors.length - 1)), step: "1", "aria-label": t.floor_label });
      winBar.out = h("output");
      winBar.title = h("h3");
      winBar.sub = h("p", { class: "sr-isub" });
      winBar.look = can("openRoom") ? h("button", { type: "button", class: "sr-btn", text: t.look_in, onclick: () => state.win && state.win.room && viewer.openRoom(state.win.room) }) : null;
      winBar.slider.addEventListener("input", () => {
        const f = floors[Number(winBar.slider.value)];
        winBar.out.textContent = String(f);
        if (can("setWindowFloor")) viewer.setWindowFloor(f);
      });
      put(ibar,
        h("div", {}, [h("p", { class: "sr-ceyebrow gold", text: t.win_view }), winBar.title, winBar.sub]),
        can("setWindowFloor") && floors.length > 1 ? h("label", { class: "sr-frow" }, [h("span", { text: t.floor_label }), winBar.slider, winBar.out]) : null,
        h("div", { class: "sr-ibtns" }, [winBar.look, leave])
      );
      updateWindowBar();
    } else if (state.mode === "room" && room) {
      put(ibar,
        h("div", {}, [h("p", { class: "sr-ceyebrow gold", text: t.schema }), h("h3", { text: fill(t.win_sub, { n: room.number, type: model.typeName(room.type_key) }) }),
          h("p", { class: "sr-isub", text: fill(t.room_sub, { side: sideText, n: room.floor }) })]),
        h("div", { class: "sr-ibtns" }, [can("openWindow") ? h("button", { type: "button", class: "sr-btn", text: t.win_view, onclick: () => viewer.openWindow(room.number) }) : null, leave])
      );
    }
  }

  let ibarKey = "";
  function render() {
    const list = levels();
    renderCrumbs(list);
    renderRail(list);
    for (const b of viewButtons) b.setAttribute("aria-pressed", String(state.activeView === b.dataset.view));
    const showCard = state.mode === "model" && !!state.selRoom && model.rooms.has(state.selRoom);
    if (showCard) roomCard(state.selRoom);
    card.hidden = !showCard;
    stage.classList.toggle("has-card", showCard);
    // Полоса режима: окно — одна на вход (этаж и номер меняют надписи), номер изнутри — на номер;
    // в перелёте ("flying") полосы нет.
    const bar = state.mode === "window" || state.mode === "room";
    const key = !bar || !state.win ? "" : state.mode === "window" ? "window" : `room:${state.win.room}`;
    if (key !== ibarKey) {
      ibarKey = key;
      if (key) renderIbar();
    } else if (key === "window") updateWindowBar();
    ibar.hidden = !key;
    wframe.hidden = state.mode !== "window";
    let k = "hint_model";
    if (state.mode === "window") k = "hint_window";
    else if (state.mode === "room") k = "hint_room";
    else if (state.selFloor !== null) k = model.isSale(state.selFloor) ? "hint_floor_open" : null;
    else if (state.tab === "floors") k = "hint_floors";
    hint.textContent = k ? t[k] : "";
    hint.hidden = !k || (k === "hint_model" && state.hintSeen);
  }

  function hover(hit) {
    if (!hit || state.mode !== "model") {
      tip.hidden = true;
      return;
    }
    const label = model.floorLabel(hit.floor);
    tip.textContent = hit.room ? `${roomN(hit.room)} · ${floorN(hit.floor)}` : label ? `${floorN(hit.floor)} · ${label}` : floorN(hit.floor);
    tip.style.left = `${hit.x}px`;
    tip.style.top = `${hit.y}px`;
    tip.hidden = false;
  }

  new ResizeObserver(() => {
    const w = stage.clientWidth;
    const was = stage.classList.contains("narrow");
    stage.classList.toggle("narrow", w < 600);
    stage.classList.toggle("tight", w < 860);
    if (was !== w < 600) render();
  }).observe(stage);

  if (!viewer) for (const node of [nav, rail, views]) node.hidden = true;
  else showCoach("auto");
  return { render, hover, nav: { goFloor, goRoom, collapse, applyView } };
}
