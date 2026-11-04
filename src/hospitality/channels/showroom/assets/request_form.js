/* Формы запроса 3D-витрины (spec 0069 §2.4, §5; PR (в), #805; вид прототипа — #819).
 *
 * Данные — JSON #showroom-request-data (`request_form.form_block`): надписи трёх языков,
 * сегодня отеля, пределы. Стык со страницей — window.Showroom (`showroom.js`):
 *   панель сама переносит узел #showroom-request в слот [data-sr-slot=request]: «Для групп» —
 *   по кнопке «Запросить эти номера», «Для гостей» — всегда;
 *   Showroom.audience() и showroom:audience — вкладка; Showroom.rooms(аудитория) и
 *   showroom:rooms — номера группы или гостя, их ведёт страница (убрать — toggleRoom);
 * и с помощником (`assistant.js`): showroom:draft {type: "draft_request", kind, guests, …} — черновик.
 * Язык — html[lang]. Черновик только заполняет форму: отправляет её кнопка посетителя (P-9).
 */
(function () {
  "use strict";
  var root = document.getElementById("showroom-request");
  var source = document.getElementById("showroom-request-data");
  if (!root || !source) return;
  var data = JSON.parse(source.textContent);
  var t = data.texts.en;
  var state = { audience: "group", draft: false, key: newKey(), values: {}, sent: null };

  function S() { return window.Showroom || null; }
  function currentLanguage() {
    var lang = (document.documentElement.lang || "en").slice(0, 2);
    return data.texts[lang] ? lang : "en";
  }
  function currentAudience() {
    var audience = S() ? S().audience() : null;
    return audience === "booking" ? "booking" : "group";
  }
  function rooms() { return S() ? S().rooms(state.audience) : []; }
  function limit() { return state.audience === "group" ? data.max_group_rooms : data.max_booking_rooms; }

  function newKey() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return "k" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  function addDays(iso, days) {
    var d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (name) {
      if (name === "text") node.textContent = attrs[name];
      else if (name === "html") node.innerHTML = attrs[name];
      else if (attrs[name] !== null && attrs[name] !== undefined) node.setAttribute(name, attrs[name]);
    });
    (children || []).forEach(function (child) { if (child) node.appendChild(child); });
    return node;
  }

  function field(name, label, input, required) {
    input.id = "sr-" + name;
    input.className = "sr-inp";
    input.name = name;
    if (state.values[name] !== undefined) input.value = state.values[name];
    input.addEventListener("input", function () { state.values[name] = input.value; });
    return el("div", { "class": "sr-fld" }, [
      el("label", { "for": input.id }, [document.createTextNode(label), required ? el("span", { "class": "sr-star", "aria-hidden": "true", text: " *" }) : null]),
      input
    ]);
  }
  function text(name, label, attrs, required) {
    return field(name, label, el("input", Object.assign({ type: "text" }, attrs || {})), required);
  }

  // Номера по этажам: «15 этаж — 1501, 1502». Этаж номера знает сцена страницы.
  function byFloor(list) {
    var model = S() && S().model;
    var floors = {};
    list.forEach(function (n) {
      var room = model && model.rooms.get(n);
      var f = room ? room.floor : 0;
      (floors[f] = floors[f] || []).push(n);
    });
    return Object.keys(floors).map(Number).sort(function (a, b) { return a - b; }).map(function (f) {
      return { floor: f, rooms: floors[f] };
    });
  }
  function floorText(f) {
    var ui = S() && S().texts;
    return ui && f ? ui.floor_n.replace("{n}", String(f)) : "";
  }

  function roomsBlock() {
    var list = rooms().slice(0, limit());
    var group = state.audience === "group";
    if (group) {
      if (!list.length) return el("p", { "class": "sr-hint", text: t.rooms_empty });
      return el("div", { "class": "sr-rqsum" }, [
        el("p", { "class": "sr-rqtotal", text: t.rooms + ": " + list.length }),
        el("ul", { "class": "sr-rqrows" }, byFloor(list).map(function (row) {
          return el("li", {}, [el("b", { text: floorText(row.floor) }), document.createTextNode(row.rooms.join(", "))]);
        }))
      ]);
    }
    // У гостя без номеров с модели вместо списка — выбор «Желаемый номер или вид».
    if (!list.length) return null;
    return el("div", { "class": "sr-fld" }, [
      el("span", { "class": "sr-lbl", text: t.wish }),
      el("ul", { "class": "sr-rooms" }, list.map(function (room) {
        var remove = el("button", { type: "button", "class": "sr-btn sr-remove", text: t.remove_room, "aria-label": t.remove_room + " " + room });
        remove.addEventListener("click", function () { if (S()) S().toggleRoom("booking", room); });
        return el("li", {}, [el("span", { text: room }), remove]);
      }))
    ]);
  }

  function sceneOptions(kind) {
    var scene = S() && S().scene;
    var items = (scene && scene[kind]) || {};
    var lang = currentLanguage();
    return Object.keys(items).map(function (key) {
      var names = items[key].names || {};
      return { key: key, name: names[lang] || names.en || key };
    });
  }
  function wishSelect() {
    var select = el("select", {}, [el("option", { value: "", text: t.wish_any })]);
    sceneOptions("views").forEach(function (view) { select.appendChild(el("option", { value: "view:" + view.key, text: view.name })); });
    sceneOptions("room_types").forEach(function (type) { select.appendChild(el("option", { value: "type:" + type.key, text: type.name })); });
    return field("wish", t.wish, select);
  }

  function render() {
    t = data.texts[currentLanguage()];
    if (state.sent) return renderSent();
    var group = state.audience === "group";
    var form = el("form", { "class": "sr-req", novalidate: "" });
    var title = state.draft ? t.draft_title : (group ? t.group_title : t.booking_title);
    var close = group ? el("button", { type: "button", "class": "sr-x", "aria-label": t.close, text: "×" }) : null;
    if (close) close.addEventListener("click", function () { if (S()) S().closeRequest(); });
    form.appendChild(el("div", { "class": "sr-rqhead" }, [el("h2", { text: title }), close]));
    if (state.draft) form.appendChild(el("p", { "class": "sr-hint", text: t.draft_note }));
    var dateMax = addDays(data.today, data.max_days_ahead);
    var maxGuests = group ? data.max_group_guests : data.max_booking_guests;
    var dates = el("div", { "class": "sr-fgrid" }, [
      field("check_in", t.check_in, el("input", { type: "date", min: data.today, max: dateMax, required: "" }), true),
      field("check_out", t.check_out, el("input", { type: "date", min: data.today, max: dateMax, required: "" }), true)
    ]);
    var guests = field("guests_count", t.guests, el("input", { type: "number", min: "1", max: String(maxGuests), required: "", inputmode: "numeric" }), true);
    var contacts = [
      group ? text("company", t.company, { maxlength: "200", required: "", autocomplete: "organization" }, true) : null,
      text("contact_name", group ? t.contact_name : t.guest_name, { maxlength: "120", required: "", autocomplete: "name" }, true),
      el("div", { "class": "sr-fgrid" }, [
        text("phone", t.phone, { type: "tel", maxlength: "32", autocomplete: "tel" }),
        text("email", t.email, { type: "email", maxlength: "254", autocomplete: "email" })
      ])
    ];
    var roomsNode = roomsBlock();
    var order = group ? [roomsNode].concat(contacts, [dates, guests]) : [dates, guests].concat(contacts, [roomsNode || wishSelect()]);
    order.forEach(function (node) { if (node) form.appendChild(node); });
    form.appendChild(field("wishes", t.wishes, el("textarea", { rows: "3", maxlength: "500", placeholder: group ? t.wishes_placeholder : null })));
    // Ловушка для ботов: человек её не видит и не заполняет.
    form.appendChild(el("input", { type: "text", name: "website", "class": "sr-trap", tabindex: "-1", autocomplete: "off", "aria-hidden": "true" }));
    form.appendChild(el("p", { "class": "sr-consent", html: t.consent_html }));
    form.appendChild(el("div", { "class": "sr-actions" }, [
      el("button", { type: "submit", "class": "sr-btn primary sr-send", text: t.send }),
      state.draft ? el("button", { type: "button", "class": "sr-btn sr-edit", text: t.edit }) : null
    ]));
    form.appendChild(el("p", { "class": "sr-hint", text: group ? t.group_note : t.booking_note }));
    form.appendChild(el("p", { "class": "sr-status", role: "status" }));
    var edit = form.querySelector(".sr-edit");
    if (edit) edit.addEventListener("click", function () { state.draft = false; render(); });
    form.addEventListener("submit", submit);
    root.replaceChildren(form);
  }

  // «4 декабря» на языке страницы; полдень — чтобы пояс браузера не сдвинул день.
  function day(iso) {
    var locale = { ru: "ru-RU", kk: "kk-KZ", en: "en-GB" }[currentLanguage()] || "en-GB";
    try {
      return new Date(iso + "T12:00:00").toLocaleDateString(locale, { day: "numeric", month: "long" });
    } catch (error) {
      return iso;
    }
  }

  // Что ушло в службу — той же карточкой, что увидит отдел продаж или бронирования.
  function renderSent() {
    var p = state.sent;
    var group = p.kind === "group";
    var facts = el("dl", { "class": "sr-ib-facts" }, [
      el("div", {}, [el("dt", { text: t.guests }), el("dd", { text: String(p.guests_count) })]),
      el("div", {}, [el("dt", { text: t.dates }), el("dd", { text: day(p.check_in) + " – " + day(p.check_out) })]),
      p.room_numbers.length ? el("div", {}, [el("dt", { text: t.rooms }), el("dd", { text: String(p.room_numbers.length) })]) : null
    ]);
    var contact = [p.contact_name, p.phone, p.email].filter(Boolean).join(" · ");
    var back = el("button", { type: "button", "class": "sr-btn primary", text: t.back });
    back.addEventListener("click", function () {
      state.sent = null;
      if (S()) S().closeRequest();
      render();
    });
    var icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("aria-hidden", "true");
    ["M12 3a9 9 0 1 0 0 18a9 9 0 1 0 0-18", "m8 12.5 2.7 2.7L16.5 9.5"].forEach(function (d) {
      var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      icon.appendChild(path);
    });
    root.replaceChildren(el("div", { "class": "sr-rqdone", role: "status" }, [
      el("p", { "class": "sr-donehead" }, [icon, el("span", { text: group ? t.group_sent : t.booking_sent })]),
      el("article", { "class": "sr-inbox", "aria-label": group ? t.group_title : t.booking_title }, [
        el("h3", { "class": "sr-ib-title", text: group ? t.group_title : t.booking_title }),
        el("div", {}, [
          group ? el("p", { "class": "sr-ib-agency", text: p.company }) : null,
          el("p", { "class": "sr-ib-contact", text: contact })
        ]),
        facts,
        p.room_numbers.length ? el("div", { "class": "sr-ib-rooms" }, byFloor(p.room_numbers).map(function (row) {
          return el("p", {}, [el("b", { text: floorText(row.floor) + " " }), document.createTextNode(row.rooms.join(", "))]);
        })) : null,
        p.wishes ? el("div", { "class": "sr-ib-comment" }, [el("p", { "class": "sr-ib-lbl", text: t.wishes }), el("p", { text: p.wishes })]) : null
      ]),
      back
    ]));
  }

  function body(form) {
    var v = state.values;
    var list = rooms().slice(0, limit());
    var payload = {
      client_key: state.key,
      kind: state.audience,
      contact_name: v.contact_name || "",
      phone: v.phone || null,
      email: v.email || null,
      guests_count: parseInt(v.guests_count, 10) || 0,
      check_in: v.check_in || null,
      check_out: v.check_out || null,
      room_numbers: list,
      wishes: v.wishes || null,
      language: currentLanguage(),
      website: form.elements.website.value
    };
    if (state.audience === "group") payload.company = v.company || "";
    var wish = v.wish || "";
    if (!list.length && wish.indexOf("view:") === 0) payload.wish_view_key = wish.slice(5);
    if (!list.length && wish.indexOf("type:") === 0) payload.wish_type_key = wish.slice(5);
    return payload;
  }

  function submit(event) {
    event.preventDefault();
    var form = event.target;
    var status = form.querySelector(".sr-status");
    var payload = body(form);
    if (!payload.phone && !payload.email) {
      status.textContent = t.need_contact;
      return;
    }
    var send = form.querySelector(".sr-send");
    send.disabled = true;
    fetch("/showroom/" + encodeURIComponent(data.slug) + "/requests", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (response) {
      if (response.status === 201 || response.status === 200) {
        state.values = {};
        state.draft = false;
        state.key = newKey();
        state.sent = payload;
        render();
        return;
      }
      send.disabled = false;
      status.textContent = response.status === 429 ? t.rate_limited
        : response.status === 422 ? t.invalid : t.network;
    }).catch(function () {
      send.disabled = false;
      status.textContent = t.network;
    });
  }

  document.addEventListener("showroom:ready", function () {
    state.audience = currentAudience();
    render();
  });
  document.addEventListener("showroom:audience", function (event) {
    var audience = event.detail === "booking" ? "booking" : "group";
    if (audience !== state.audience) {
      state.audience = audience;
      state.draft = false;
      state.sent = null;
      render();
    }
  });
  document.addEventListener("showroom:rooms", function (event) {
    if (event.detail && event.detail.audience === state.audience && !state.sent) render();
  });
  document.addEventListener("showroom:draft", function (event) {
    var d = event.detail || {};
    if (d.type !== "draft_request" || !S()) return;
    var kind = d.kind === "booking" ? "booking" : "group";
    // Вкладка страницы следует за видом черновика; у группы форма открывается в панели.
    S().openRequest(kind);
    state.audience = kind;
    state.draft = true;
    state.sent = null;
    if (d.check_in) state.values.check_in = d.check_in;
    if (d.check_out) state.values.check_out = d.check_out;
    if (d.guests) state.values.guests_count = String(d.guests);
    if (d.wishes) state.values.wishes = d.wishes;
    // Черновик — слова посетителя: свои номера заменяют выбор на модели, вид или тип без
    // номеров — тоже (иначе форма показала бы прежние номера вместо названного вида).
    if (Array.isArray(d.room_numbers) && d.room_numbers.length) S().setRooms(kind, d.room_numbers.map(String));
    else if (d.view_key || d.type_key) S().setRooms(kind, [], { fly: false });
    if (d.view_key) state.values.wish = "view:" + d.view_key;
    else if (d.type_key) state.values.wish = "type:" + d.type_key;
    render();
    var first = root.querySelector("#sr-company") || root.querySelector("#sr-contact_name");
    if (first) first.focus({ preventScroll: true });
  });

  render();
})();
