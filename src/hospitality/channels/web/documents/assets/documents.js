// «Реквизиты для закрывающих документов» (spec 0049 §6). Номер и даты — JSON
// `…/documents/data`, отправка — multipart `POST …/documents` (Р-8). Имена
// файлов и текст гостя выводятся только через esc().
"use strict";
(() => {
  const T = JSON.parse(document.getElementById("documents-texts").textContent);
  const $ = (s, r) => (r || document).querySelector(s);
  const base = location.pathname.replace(/\/documents\/?$/, "");
  const app = $("#app"), bar = $("#bar"), barInner = $("#bar-inner");
  const MAX_FILES = 5, DOC_MAX = 10 * 1024 * 1024, PHOTO_MAX = 5 * 1024 * 1024, PHOTO_SIDE = 2400;
  // Без HEIC (Р-8): iPhone тогда сам отдаёт JPEG.
  const KINDS = {pdf: "pdf", docx: "doc", jpg: "img", jpeg: "img", png: "img", webp: "img"};
  const MIME_KINDS = {"application/pdf": "pdf", "image/jpeg": "img", "image/png": "img", "image/webp": "img",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "doc"};
  const SENT_FLAG = "necturn:documents-sent";
  const ERRORS = {"ERR-DOCUMENTS-001": "err_empty", "ERR-DOCUMENTS-002": "err_too_many_files",
    "ERR-DOCUMENTS-003": "err_stay_limit", "ERR-DOCUMENTS-005": "err_disabled",
    "ERR-FILES-001": "err_too_large", "ERR-FILES-002": "err_format", "ERR-FILES-003": "err_macros"};
  const state = {lang: "en", data: null, screen: "loading", billTo: "", note: "", files: [],
    error: "", sending: false, key: newKey(), sent: null};

  // ---------- утилиты ----------
  const ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"};
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ESC[c]);
  function t(key, vars) {
    const entry = T[key];
    let s = entry ? (entry[state.lang] || entry.en) : key;
    for (const [k, v] of Object.entries(vars || {})) s = s.split("{" + k + "}").join(String(v));
    return s;
  }
  const svg = (d, w) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="' + (w || 2) +
    '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + d + "</svg>";
  const I = {
    back: svg('<path d="M15 5l-7 7 7 7"/>', 2.2),
    door: svg('<path d="M5 21V4a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v17M3 21h18"/><circle cx="15" cy="12.5" r="1" fill="currentColor"/>'),
    cal: svg('<rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M3 10h18M8 3v4M16 3v4"/>'),
    up: svg('<path d="M12 16V5M6 11l6-6 6 6M5 20h14"/>', 2.2),
    x: svg('<path d="M6 6l12 12M18 6L6 18"/>', 2.4),
    check: svg('<path d="M5 12.5l4.5 4.5L19 7.5"/>', 2.6),
    lock: svg('<rect x="5" y="11" width="14" height="10" rx="2.5"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>'),
    chat: svg('<path d="M4 5h16v11H9l-5 4z"/>'),
  };
  function newKey() {
    const raw = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(36).slice(2);
    return raw.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64);
  }
  const ext = (name) => { const m = /\.([a-z0-9]+)$/i.exec(name || ""); return m ? m[1].toLowerCase() : ""; };
  const kindOf = (file) => KINDS[ext(file.name)] || MIME_KINDS[file.type] || null;
  function size(bytes) {
    const [kb, mb] = state.lang === "en" ? ["KB", "MB"] : ["КБ", "МБ"];
    if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + " " + kb;
    const value = (bytes / 1024 / 1024).toFixed(1);
    return (state.lang === "en" ? value : value.replace(".", ",")) + " " + mb;
  }
  const remember = () => { try { localStorage.setItem(SENT_FLAG, "1"); } catch (e) { /* без хранилища — подсказка останется */ } };

  // ---------- данные ----------
  async function load() {
    let response = null, data = null;
    try {
      const lang = encodeURIComponent(navigator.language || "");
      response = await fetch(base + "/documents/data?language=" + lang, {headers: {Accept: "application/json"}});
      data = await response.json();
    } catch (e) { /* сеть или не JSON — ниже */ }
    if (response && response.status === 401) { location.href = base; return; }
    if (!response || !response.ok || !data) {
      state.lang = (navigator.language || "en").slice(0, 2);
      state.screen = response && response.status === 404 ? "disabled" : "failed";
    } else {
      state.lang = data.language; state.data = data; state.screen = "form";
    }
    render();
  }

  // ---------- экраны ----------
  function render() {
    document.documentElement.lang = state.lang;
    if (state.screen === "form") renderForm();
    else if (state.screen === "done") renderDone();
    else if (state.screen === "loading") { app.innerHTML = ""; bar.hidden = true; }
    else renderNotice(state.screen === "disabled" ? "err_disabled" : "err_failed");
  }
  function topbar() {
    return '<header class="topbar"><a class="icon-btn" href="' + esc(base) + '" aria-label="' + esc(t("back_chat")) +
      '">' + I.back + '</a><div class="topbar-title"><b>' + esc(t("topbar")) + "</b></div></header>";
  }
  function renderNotice(key) {
    app.innerHTML = topbar() + '<p class="notice">' + esc(t(key)) + "</p>";
    bar.hidden = true;
  }
  const stayDates = () => state.data.check_in_label + " — " + state.data.check_out_label;

  function renderForm() {
    const d = state.data;
    const chips = state.files.map((f, i) =>
      '<div class="chip"><span class="kind ' + f.kind + '">' + {pdf: "PDF", doc: "DOC", img: "IMG"}[f.kind] +
      "</span><div><b>" + esc(f.name) + "</b><span>" + size(f.file.size) + '</span></div><button type="button" data-rm="' +
      i + '" aria-label="' + esc(t("remove_file")) + '">' + I.x + "</button></div>").join("");
    app.innerHTML = topbar() +
      '<h1 class="screen-title">' + esc(t("title")) + '</h1><p class="lead">' + esc(t("lead")) + "</p>" +
      '<div class="card rows">' +
      '<div class="info-row"><span class="info-ico">' + I.door + "</span><div><b>" + esc(t("room", {room: d.room_number})) +
      "</b><span>" + esc(t("room_sub")) + "</span></div></div>" +
      '<div class="info-row"><span class="info-ico">' + I.cal + "</span><div><b>" + esc(stayDates()) +
      "</b><span>" + esc(t("dates_sub")) + "</span></div></div></div>" +
      '<label class="label" for="bill-to">' + esc(t("bill_to_label")) + "</label>" +
      '<input type="text" id="bill-to" maxlength="500" autocomplete="organization" placeholder="' +
      esc(t("bill_to_placeholder")) + '" value="' + esc(state.billTo) + '">' +
      '<div class="drop" id="drop"><span class="big">' + I.up + "</span><b>" + esc(t("files_title")) + "</b><span>" +
      esc(t("files_sub")) + '</span><label class="pick">' + esc(t("files_pick")) +
      '<input type="file" id="file" multiple accept=".pdf,.docx,image/jpeg,image/png,image/webp"></label></div>' +
      '<div class="chips">' + chips + "</div>" +
      (state.error ? '<p class="hint bad" role="alert">' + esc(state.error) + "</p>" : "") +
      '<label class="label" for="note">' + esc(t("note_label")) + "</label>" +
      '<textarea id="note" maxlength="2000" placeholder="' + esc(t("note_placeholder")) + '">' + esc(state.note) + "</textarea>" +
      '<p class="hint">' + esc(t("note_hint")) + "</p>" +
      '<p class="privacy">' + I.lock + "<span>" + esc(t("privacy")) + "</span></p>";
    bar.hidden = false;
    barInner.innerHTML = '<button class="cta" type="button" data-act="submit"' + (state.sending ? " disabled" : "") + ">" +
      (state.sending ? '<span class="spin" aria-hidden="true"></span><span>' + esc(t("sending")) + "</span>"
        : "<span>" + esc(t("submit")) + "</span>") + "</button>";
    wireForm();
  }
  function wireForm() {
    const input = $("#file"), drop = $("#drop"), billTo = $("#bill-to"), note = $("#note");
    input.addEventListener("change", () => { const list = Array.from(input.files); input.value = ""; addFiles(list); });
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => { if (e.dataTransfer) addFiles(Array.from(e.dataTransfer.files)); });
    billTo.addEventListener("input", () => { state.billTo = billTo.value; });
    note.addEventListener("input", () => { state.note = note.value; });
  }

  function renderDone() {
    const s = state.sent, d = state.data;
    const files = s.files.length ? s.files.map(esc).join(", ") : esc(t("sum_none"));
    app.innerHTML = topbar() +
      '<div class="status-head"><span class="status-ico">' + I.check + "</span><h1>" + esc(t("sent_title")) + "</h1><p>" +
      esc(t("sent_text", {date: s.checkOut})) + "</p></div>" +
      '<div class="card sum">' +
      (s.billTo ? '<div class="sum-row"><span>' + esc(t("sum_bill_to")) + "</span><span>" + esc(s.billTo) + "</span></div>" : "") +
      '<div class="sum-row"><span>' + esc(t("sum_files")) + "</span><span>" + files + "</span></div>" +
      (s.note ? '<div class="sum-row"><span>' + esc(t("sum_text")) + "</span><span>" + esc(s.note) + "</span></div>" : "") +
      '<div class="sum-row"><span>' + esc(t("sum_room")) + "</span><span>" + esc(d.room_number + " · " + stayDates()) + "</span></div>" +
      "</div>" +
      '<button class="ghost-btn" type="button" data-act="more">' + esc(t("send_more")) + "</button>";
    bar.hidden = false;
    barInner.innerHTML = '<a class="cta" href="' + esc(base) + '">' + I.chat + "<span>" + esc(t("back_to_chat")) + "</span></a>";
  }

  // ---------- файлы ----------
  async function addFiles(list) {
    state.error = "";
    for (const file of list) {
      const kind = kindOf(file);
      if (!kind) { state.error = t("err_format", {name: file.name}); continue; }
      if (state.files.length >= MAX_FILES) { state.error = t("err_too_many_files"); break; }
      let ready = file;
      // Снимок телефона больше 5 МБ уменьшается здесь, а не получает отказ (Р-8).
      if (kind === "img" && file.size > PHOTO_MAX) ready = await shrinkPhoto(file);
      if (ready.size > (kind === "img" ? PHOTO_MAX : DOC_MAX)) { state.error = t("err_too_large", {name: file.name}); continue; }
      state.files.push({file: ready, name: ready.name, kind});
    }
    render();
  }
  async function shrinkPhoto(file) {
    try {
      const bitmap = await createImageBitmap(file);
      const scale = Math.min(1, PHOTO_SIDE / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(bitmap.width * scale); canvas.height = Math.round(bitmap.height * scale);
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      for (const quality of [0.85, 0.7]) {
        const blob = await new Promise((done) => canvas.toBlob(done, "image/jpeg", quality));
        if (blob && blob.size <= PHOTO_MAX) {
          return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", {type: "image/jpeg"});
        }
      }
    } catch (e) { /* браузер не смог — отказ размером ниже */ }
    return file;
  }

  // ---------- отправка ----------
  async function submit() {
    if (state.sending) return;
    if (!state.billTo.trim() && !state.note.trim() && !state.files.length) {
      state.error = t("err_empty"); render(); $("#bill-to").focus(); return;
    }
    state.error = ""; state.sending = true; render();
    const form = new FormData();
    form.append("client_key", state.key);
    form.append("language", state.lang);
    form.append("bill_to", state.billTo);
    form.append("note", state.note);
    for (const f of state.files) form.append("files", f.file, f.name);
    let response = null, data = null;
    try {
      response = await fetch(base + "/documents", {method: "POST", body: form});
      data = await response.json();
    } catch (e) { /* сеть или не JSON — ниже */ }
    state.sending = false;
    if (response && response.status === 401) { location.href = base; return; }
    if (response && response.ok && data) {
      state.sent = {billTo: state.billTo.trim(), note: state.note.trim(), files: state.files.map((f) => f.name),
        checkOut: data.check_out_label};
      state.billTo = ""; state.note = ""; state.files = []; state.key = newKey();
      remember(); state.screen = "done"; render(); window.scrollTo(0, 0);
      return;
    }
    state.error = errorText(data && data.error);
    render();
  }
  function errorText(error) {
    const key = (error && ERRORS[error.code]) || "err_failed";
    const position = error && error.details && error.details[0] ? error.details[0].file_position : null;
    const file = position != null ? state.files[position] : null;
    return t(key, {name: file ? file.name : ""});
  }

  // ---------- события ----------
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-act], [data-rm]");
    if (!el) return;
    if (el.dataset.rm !== undefined) { state.files.splice(Number(el.dataset.rm), 1); state.error = ""; render(); return; }
    if (el.dataset.act === "submit") submit();
    else if (el.dataset.act === "more") { state.screen = "form"; state.error = ""; render(); window.scrollTo(0, 0); }
  });
  load();
})();
