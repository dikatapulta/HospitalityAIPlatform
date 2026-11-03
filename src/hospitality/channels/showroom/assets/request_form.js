/* Формы запроса 3D-витрины (spec 0069 §2.4, §5; PR (в), #805).
 *
 * Данные — JSON #showroom-request-data (`request_form.form_block`): надписи трёх языков,
 * сегодня отеля, пределы, виды и типы номеров сцены. Стык со страницей (а) — `showroom.js`:
 *   showroom:ready — блок переезжает в слот [data-sr-slot=request];
 *   showroom:audience (detail "group"|"booking") и Showroom.audience() — вкладка;
 *   showroom:select (detail — номер) — номер, выбранный на модели, встаёт в форму;
 * и с помощником (б): showroom:command {type: "draft_request", kind, guests, …} — черновик.
 * Язык — html[lang]. Черновик только заполняет форму: отправляет её кнопка посетителя (P-9).
 */
(function () {
  "use strict";
  var root = document.getElementById("showroom-request");
  var source = document.getElementById("showroom-request-data");
  if (!root || !source) return;
  var data = JSON.parse(source.textContent);
  var t = data.texts.en;
  var state = {
    audience: currentAudience(),
    rooms: [],
    draft: false,
    key: newKey(),
    values: {}
  };

  function currentLanguage() {
    var lang = (document.documentElement.lang || "en").slice(0, 2);
    return data.texts[lang] ? lang : "en";
  }

  function currentAudience() {
    var audience = window.Showroom && window.Showroom.audience ? window.Showroom.audience() : null;
    return audience === "booking" ? "booking" : "group";
  }

  function mount() {
    var slot = window.Showroom && window.Showroom.slot ? window.Showroom.slot("request") : null;
    if (slot && root.parentNode !== slot) slot.appendChild(root);
  }

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

  function field(name, label, input) {
    input.id = "sr-" + name;
    input.className = "sr-input";
    input.name = name;
    if (state.values[name] !== undefined) input.value = state.values[name];
    input.addEventListener("input", function () { state.values[name] = input.value; });
    return el("label", { "class": "sr-field", "for": input.id }, [
      el("span", { text: label }), input
    ]);
  }

  function text(name, label, attrs) {
    return field(name, label, el("input", Object.assign({ type: "text" }, attrs || {})));
  }

  function maxGuests() {
    return state.audience === "group" ? data.max_group_guests : data.max_booking_guests;
  }

  function roomsBlock() {
    var limit = state.audience === "group" ? data.max_group_rooms : data.max_booking_rooms;
    var rooms = state.rooms.slice(0, limit);
    var list = el("ul", { "class": "sr-rooms" }, rooms.map(function (room) {
      var remove = el("button", { type: "button", "class": "sr-btn sr-remove", text: t.remove_room,
        "aria-label": t.remove_room + " " + room });
      remove.addEventListener("click", function () {
        state.rooms = state.rooms.filter(function (r) { return r !== room; });
        render();
      });
      return el("li", {}, [el("span", { text: room }), remove]);
    }));
    var group = state.audience === "group";
    // У гостя без номеров с модели вместо списка — выбор «Желаемый номер или вид».
    if (!group && !rooms.length) return null;
    return el("div", { "class": "sr-field" }, [
      el("span", { text: group ? t.rooms : t.wish }),
      rooms.length ? list : el("p", { "class": "sr-hint", text: t.rooms_empty })
    ]);
  }

  function sceneOptions(kind) {
    // Виды и типы номеров — из сцены, которую страница уже загрузила (window.Showroom.scene).
    var scene = window.Showroom && window.Showroom.scene;
    var items = (scene && scene[kind]) || {};
    var lang = currentLanguage();
    return Object.keys(items).map(function (key) {
      var names = items[key].names || {};
      return { key: key, name: names[lang] || names.en || key };
    });
  }

  function wishSelect() {
    var select = el("select", {}, [el("option", { value: "", text: t.wish_any })]);
    sceneOptions("views").forEach(function (view) {
      select.appendChild(el("option", { value: "view:" + view.key, text: view.name }));
    });
    sceneOptions("room_types").forEach(function (type) {
      select.appendChild(el("option", { value: "type:" + type.key, text: type.name }));
    });
    return field("wish", t.wish, select);
  }

  function render() {
    t = data.texts[currentLanguage()];
    var group = state.audience === "group";
    var form = el("form", { "class": "sr-form", novalidate: "" });
    var title = state.draft ? t.draft_title : (group ? t.group_title : t.booking_title);
    form.appendChild(el("h2", { "class": "sr-title", text: title }));
    if (state.draft) form.appendChild(el("p", { "class": "sr-hint", text: t.draft_note }));
    var dateMax = addDays(data.today, data.max_days_ahead);
    var dates = [
      field("check_in", t.check_in, el("input", { type: "date", min: data.today, max: dateMax, required: "" })),
      field("check_out", t.check_out, el("input", { type: "date", min: data.today, max: dateMax, required: "" })),
      field("guests_count", t.guests, el("input", { type: "number", min: "1", max: String(maxGuests()), required: "", inputmode: "numeric" }))
    ];
    var contacts = [
      group ? text("company", t.company, { maxlength: "200", required: "", autocomplete: "organization" }) : null,
      text("contact_name", group ? t.contact_name : t.guest_name, { maxlength: "120", required: "", autocomplete: "name" }),
      text("phone", t.phone, { type: "tel", maxlength: "32", autocomplete: "tel" }),
      text("email", t.email, { type: "email", maxlength: "254", autocomplete: "email" })
    ];
    var order = group ? contacts.concat(dates) : dates.concat(contacts);
    order.forEach(function (node) { if (node) form.appendChild(node); });
    var rooms = roomsBlock();
    form.appendChild(rooms || wishSelect());
    form.appendChild(field("wishes", t.wishes, el("textarea", { rows: "3", maxlength: "500", placeholder: group ? t.wishes_placeholder : null })));
    // Ловушка для ботов: человек её не видит и не заполняет.
    form.appendChild(el("input", { type: "text", name: "website", "class": "sr-trap", tabindex: "-1", autocomplete: "off", "aria-hidden": "true" }));
    form.appendChild(el("p", { "class": "sr-consent", html: t.consent_html }));
    var buttons = el("div", { "class": "sr-actions" }, [
      el("button", { type: "submit", "class": "sr-btn sr-btn-primary sr-send", text: t.send }),
      state.draft ? el("button", { type: "button", "class": "sr-btn sr-edit", text: t.edit }) : null
    ]);
    form.appendChild(buttons);
    form.appendChild(el("p", { "class": "sr-hint", text: group ? t.group_note : t.booking_note }));
    form.appendChild(el("p", { "class": "sr-status", role: "status" }));
    var edit = form.querySelector(".sr-edit");
    if (edit) edit.addEventListener("click", function () { state.draft = false; render(); });
    form.addEventListener("submit", submit);
    root.replaceChildren(form);
  }

  function body(form) {
    var v = state.values;
    var payload = {
      client_key: state.key,
      kind: state.audience,
      contact_name: v.contact_name || "",
      phone: v.phone || null,
      email: v.email || null,
      guests_count: parseInt(v.guests_count, 10) || 0,
      check_in: v.check_in || null,
      check_out: v.check_out || null,
      room_numbers: state.rooms.slice(0, state.audience === "group" ? data.max_group_rooms : data.max_booking_rooms),
      wishes: v.wishes || null,
      language: currentLanguage(),
      website: form.elements.website.value
    };
    if (state.audience === "group") payload.company = v.company || "";
    var wish = v.wish || "";
    if (!state.rooms.length && wish.indexOf("view:") === 0) payload.wish_view_key = wish.slice(5);
    if (!state.rooms.length && wish.indexOf("type:") === 0) payload.wish_type_key = wish.slice(5);
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
        var sent = state.audience === "group" ? t.group_sent : t.booking_sent;
        state.values = {};
        state.draft = false;
        state.key = newKey();
        render();
        root.querySelector(".sr-status").textContent = sent;
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
    mount();
    state.audience = currentAudience();
    render();
  });

  document.addEventListener("showroom:audience", function (event) {
    var audience = event.detail === "booking" ? "booking" : "group";
    if (audience !== state.audience) {
      state.audience = audience;
      state.draft = false;
      render();
    }
  });

  document.addEventListener("showroom:select", function (event) {
    var room = event.detail === undefined || event.detail === null ? "" : String(event.detail);
    var limit = state.audience === "group" ? data.max_group_rooms : data.max_booking_rooms;
    if (!room || state.rooms.indexOf(room) >= 0 || state.rooms.length >= limit) return;
    state.rooms = state.rooms.concat([room]);
    render();
  });

  document.addEventListener("showroom:command", function (event) {
    var d = event.detail || {};
    if (d.type !== "draft_request") return;
    state.audience = d.kind === "booking" ? "booking" : "group";
    // Вкладка страницы следует за видом черновика: её кнопка — у (а), она же шлёт showroom:audience.
    var tabs = document.querySelectorAll(".sr-audience button");
    var tab = tabs[state.audience === "booking" ? 1 : 0];
    if (tab && tab.getAttribute("aria-pressed") !== "true") tab.click();
    state.draft = true;
    if (d.check_in) state.values.check_in = d.check_in;
    if (d.check_out) state.values.check_out = d.check_out;
    if (d.guests) state.values.guests_count = String(d.guests);
    if (d.wishes) state.values.wishes = d.wishes;
    if (Array.isArray(d.room_numbers) && d.room_numbers.length) state.rooms = d.room_numbers.map(String);
    if (d.view_key) state.values.wish = "view:" + d.view_key;
    else if (d.type_key) state.values.wish = "type:" + d.type_key;
    render();
    root.scrollIntoView({ behavior: "smooth", block: "start" });
    var first = root.querySelector("#sr-company") || root.querySelector("#sr-contact_name");
    if (first) first.focus({ preventScroll: true });
  });

  mount();
  render();
})();
