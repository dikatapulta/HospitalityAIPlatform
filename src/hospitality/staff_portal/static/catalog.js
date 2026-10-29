/* Панель «Товары и меню» (spec 0038 §6 PR 4, issue #418).
 *
 * Ванильный JS без сборки (ADR-014), отдельный файл — CSP default-src 'self'.
 * Разметка каталога живёт только в Jinja (_catalog_board.html): после каждого
 * действия JS заменяет innerHTML #catalog-board ответом fragment-эндпоинта и
 * заново применяет поиск, фильтр и выбор. Тексты отеля JS пишет только через
 * textContent и value — никогда через innerHTML (названия ведёт персонал).
 * Поллинга нет: каталог правят редко, и вкладка не должна дёргать БД.
 *
 * CSRF-контракт JSON-действий (докстринг router.py): каждый POST несёт
 * Content-Type: application/json — Origin браузер добавляет сам.
 * Поле «Фото» формы (PR 7) — catalog_photo.js (window.CatalogPhoto): фото
 * уходит после сохранения полей товара, тем же нажатием «Сохранить».
 */
(function () {
  "use strict";

  var board = document.getElementById("catalog-board");
  var bulk = document.getElementById("catalog-bulk");
  var drawer = document.getElementById("catalog-drawer");
  var scrim = document.getElementById("catalog-scrim");
  var form = document.getElementById("catalog-form");
  var toastEl = document.getElementById("catalog-toast");
  if (!board || !bulk || !drawer || !scrim || !form || !toastEl) return;
  var endpoint = board.dataset.endpoint;
  var fragmentUrl = board.dataset.fragmentUrl;
  var photo = window.CatalogPhoto;

  var MAX_PRICE_TENGE = 1000000;
  var MINOR_PER_TENGE = 100;

  /* Дружелюбные тексты по кодам каталога ошибок (R-8). */
  var MESSAGES = {
    "ERR-ROOMSERVICE-001": "Этого раздела или товара уже нет — список обновлён.",
    "ERR-ROOMSERVICE-002": "Заказы в номер у отеля выключены — обновите страницу.",
    "ERR-AUTH-003": "Этот каталог ведёт другая служба — обновите страницу.",
    "ERR-AUTH-002": "Сессия истекла — войдите заново.",
    "ERR-PLATFORM-002": "Проверьте поля: название на английском до 120 знаков, " +
      "цена — целыми тенге до 1 000 000 ₸.",
    "ERR-FILES-001": "Фото больше 5 МБ — сожмите его на телефоне и загрузите снова.",
    "ERR-FILES-002": "Такой файл не подходит. Нужна фотография: JPG, PNG или WebP.",
    "ERR-FILES-003": "Такой файл не подходит. Нужна фотография: JPG, PNG или WebP.",
    "ERR-FILES-005": "Фото сейчас не сохранить — попробуйте позже.",
    "ERR-FILES-006": "Фото сейчас не сохранить — попробуйте позже."
  };

  /* `typed` — строка поиска как набрана: поле живёт во фрагменте и после
   * каждого действия рисуется заново пустым. */
  var state = { filter: "all", query: "", typed: "", selected: {} };
  var editing = null;   // открытая форма: { kind, id, names, descriptions, ... }
  var opener = null;    // кнопка, открывшая форму, — вернуть ей фокус

  /* ---------- связь ---------- */

  async function send(path, body) {
    try {
      var response = await fetch(endpoint + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      var data = null;
      try { data = await response.json(); } catch (error) { /* не JSON */ }
      var code = !response.ok && data && data.error ? data.error.code : "";
      return { ok: response.ok, status: response.status, code: code, data: data };
    } catch (error) {
      return { ok: false, status: 0, code: "network", data: null };
    }
  }

  function failureText(result) {
    if (result.code === "network") return "Нет связи — попробуйте ещё раз.";
    return MESSAGES[result.code] ||
      "Не получилось (" + (result.code || result.status) + "). Попробуйте ещё раз.";
  }

  async function refresh() {
    try {
      var response = await fetch(fragmentUrl, { cache: "no-store" });
      if (response.redirected) {
        /* Сессия истекла: фрагмент ответил редиректом на вход — уходим туда же. */
        window.location.href = response.url;
        return;
      }
      if (response.ok) board.innerHTML = await response.text();
    } catch (error) {
      /* Нет сети — каталог останется прежним до следующего действия. */
    }
    applyView();
  }

  var toastTimer = null;
  function toast(text) {
    toastEl.textContent = text;
    toastEl.classList.add("is-shown");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("is-shown"); }, 2200);
  }

  /* ---------- поиск, фильтр, выбор ---------- */

  function cards() { return board.querySelectorAll("[data-item-id]"); }

  function matches(card) {
    if (state.query && card.dataset.search.indexOf(state.query) < 0) return false;
    var hidden = card.dataset.hidden === "true";
    if (state.filter === "hidden") return hidden;
    if (hidden) return false;
    if (state.filter === "sold-out") return card.dataset.soldOut === "true";
    if (state.filter === "no-photo") return card.dataset.noPhoto === "true";
    return true;
  }

  function applyView() {
    var narrowed = state.filter !== "all" || state.query !== "";
    var shown = 0;
    var present = {};
    board.querySelectorAll("[data-section-id]").forEach(function (section) {
      var visible = 0;
      section.querySelectorAll("[data-item-id]").forEach(function (card) {
        var id = card.dataset.itemId;
        present[id] = true;
        var match = matches(card);
        card.hidden = !match;
        if (match) visible += 1;
        var selected = !!state.selected[id];
        card.classList.toggle("is-selected", selected);
        card.querySelector("[data-act=select]").setAttribute("aria-checked", String(selected));
      });
      section.querySelector("[data-act=add-item]").hidden = narrowed;
      section.hidden = narrowed && visible === 0;
      shown += visible;
    });
    Object.keys(state.selected).forEach(function (id) { if (!present[id]) delete state.selected[id]; });
    var empty = board.querySelector("[data-empty-filter]");
    if (empty) empty.hidden = !(narrowed && shown === 0);
    board.querySelectorAll("[data-filter]").forEach(function (chip) {
      chip.setAttribute("aria-pressed", String(chip.dataset.filter === state.filter));
    });
    var search = board.querySelector("[data-search-input]");
    if (search && search.value !== state.typed) search.value = state.typed;
    renderBulk();
  }

  function selectedIds() {
    return Object.keys(state.selected).filter(function (id) { return state.selected[id]; });
  }

  function renderBulk() {
    var count = selectedIds().length;
    bulk.hidden = count === 0;
    bulk.querySelector("[data-bulk-count]").textContent = "Выбрано: " + count;
  }

  /* Фото карточки не загрузилось (хранилище недоступно) — картинка уходит,
   * под ней буква-заглушка. error не всплывает — ловим на погружении. */
  board.addEventListener("error", function (event) {
    if (event.target.matches && event.target.matches("img[data-photo]")) event.target.remove();
  }, true);

  board.addEventListener("input", function (event) {
    if (!event.target.matches("[data-search-input]")) return;
    state.typed = event.target.value;
    state.query = state.typed.trim().toLowerCase();
    applyView();
  });

  /* ---------- меню карточки ---------- */

  function closeMenus() {
    board.querySelectorAll(".cat-menu").forEach(function (menu) {
      if (menu.hidden) return;
      menu.hidden = true;
      var toggle = menu.parentElement.querySelector("[data-act=menu]");
      if (toggle) toggle.setAttribute("aria-expanded", "false");
    });
  }

  function toggleMenu(button) {
    var menu = button.parentElement.querySelector(".cat-menu");
    var open = menu.hidden;
    closeMenus();
    if (!open) return;
    menu.hidden = false;
    button.setAttribute("aria-expanded", "true");
    var first = menu.querySelector("button:not(:disabled)");
    if (first) first.focus();
  }

  /* ---------- действия без формы ---------- */

  async function act(button, path, body, done) {
    button.disabled = true;
    var result = await send(path, body);
    toast(result.ok ? done : failureText(result));
    await refresh();
  }

  function cardForm(card) { return JSON.parse(card.dataset.form); }

  board.addEventListener("click", function (event) {
    var chip = event.target.closest("[data-filter]");
    if (chip) {
      state.filter = chip.dataset.filter;
      return applyView();
    }
    var button = event.target.closest("[data-act]");
    if (!button || button.disabled) return;
    var action = button.dataset.act;
    var card = button.closest("[data-item-id]");
    var section = button.closest("[data-section-id]");
    var toolbar = button.closest("[data-catalog]");
    if (action !== "menu") closeMenus();
    if (action === "menu") return toggleMenu(button);
    if (action === "select") {
      var id = card.dataset.itemId;
      state.selected[id] = !state.selected[id];
      return applyView();
    }
    if (action === "accepting") {
      var accepting = button.getAttribute("aria-checked") !== "true";
      return act(button, "/catalogs/" + toolbar.dataset.catalog + "/accepting",
        { accepting: accepting },
        accepting ? "Заказы в номер принимаются" : "Заказы в номер не принимаются");
    }
    if (action === "available") {
      var available = button.getAttribute("aria-checked") !== "true";
      return act(button, "/items/" + card.dataset.itemId + "/available", { available: available },
        available ? "Снова в наличии" : "Гость увидит «Закончилось»");
    }
    if (action === "hide" || action === "unhide") {
      var hide = action === "hide";
      return act(button, "/items/" + card.dataset.itemId + "/hidden", { hidden: hide },
        hide ? "Скрыто из меню" : "Снова в меню");
    }
    if (action === "item-up" || action === "item-down") {
      return act(button, "/items/" + card.dataset.itemId + "/move",
        { direction: action === "item-up" ? "up" : "down" }, "Порядок изменён");
    }
    if (action === "section-up" || action === "section-down") {
      return act(button, "/sections/" + section.dataset.sectionId + "/move",
        { direction: action === "section-up" ? "up" : "down" }, "Порядок изменён");
    }
    if (action === "rename-section") {
      return openSection(button, section.dataset.sectionId, JSON.parse(section.dataset.names));
    }
    if (action === "add-item") return openItem(button, null, section.dataset.sectionId);
    if (action === "edit") return openItem(button, cardForm(card), null);
    if (action === "photo") {
      openItem(button, cardForm(card), null);
      if (photo) photo.pick();
      return;
    }
    if (action === "move") {
      openItem(button, cardForm(card), null);
      document.getElementById("catalog-section").focus();
    }
  });

  document.addEventListener("click", function (event) {
    if (!event.target.closest(".cat-menu, [data-act=menu]")) closeMenus();
    var head = event.target.closest(".cat-head [data-act]");
    if (!head) return;
    if (head.dataset.act === "add-section") openSection(head, null, {});
    if (head.dataset.act === "add-item") openItem(head, null, null);
  });

  /* ---------- массовые действия ---------- */

  var BULK = {
    "sold-out": { field: "available", value: false },
    "available": { field: "available", value: true },
    "hide": { field: "hidden", value: true }
  };

  bulk.addEventListener("click", async function (event) {
    var button = event.target.closest("[data-bulk]");
    if (!button) return;
    var kind = button.dataset.bulk;
    var ids = selectedIds();
    state.selected = {};
    if (kind === "clear") return applyView();
    var spec = BULK[kind];
    bulk.querySelectorAll("button").forEach(function (each) { each.disabled = true; });
    var done = 0;
    var failure = null;
    for (var i = 0; i < ids.length; i += 1) {
      var body = {};
      body[spec.field] = spec.value;
      var result = await send("/items/" + ids[i] + "/" + spec.field, body);
      if (result.ok) done += 1;
      else if (!failure) failure = result;
    }
    bulk.querySelectorAll("button").forEach(function (each) { each.disabled = false; });
    toast(failure ? "Готово: " + done + " из " + ids.length + ". " + failureText(failure)
      : "Готово: " + done);
    await refresh();
  });

  /* ---------- выдвижная форма: товар и раздел ---------- */

  var title = document.getElementById("catalog-drawer-title");
  var nameInput = document.getElementById("catalog-name");
  var descInput = document.getElementById("catalog-desc");
  var priceInput = document.getElementById("catalog-price");
  var sectionSelect = document.getElementById("catalog-section");
  var availableToggle = document.getElementById("catalog-available");
  var formError = document.getElementById("catalog-form-error");
  var saveButton = document.getElementById("catalog-save");

  function sectionOptions(selectedId) {
    while (sectionSelect.firstChild) sectionSelect.removeChild(sectionSelect.firstChild);
    board.querySelectorAll("[data-section-id]").forEach(function (section) {
      var option = document.createElement("option");
      option.value = section.dataset.sectionId;
      option.textContent = section.querySelector("h2").textContent;
      option.selected = option.value === selectedId;
      sectionSelect.appendChild(option);
    });
  }

  function setLanguage(language) {
    if (editing.language) {
      editing.names[editing.language] = nameInput.value;
      editing.descriptions[editing.language] = descInput.value;
    }
    editing.language = language;
    nameInput.value = editing.names[language] || "";
    descInput.value = editing.descriptions[language] || "";
    form.querySelectorAll("[data-lang]").forEach(function (tab) {
      tab.setAttribute("aria-selected", String(tab.dataset.lang === language));
    });
  }

  function showError(text) {
    formError.textContent = text;
    formError.hidden = !text;
  }

  function setAvailable(available) {
    editing.available = available;
    availableToggle.setAttribute("aria-checked", String(available));
    form.querySelector("[data-available-label]").textContent =
      available ? "В наличии" : "Закончилось";
  }

  function openDrawer(button, data) {
    editing = data;
    opener = button;
    showError("");
    form.querySelectorAll("[data-item-only]").forEach(function (el) { el.hidden = data.kind !== "item"; });
    var hideable = data.kind === "item" && !!data.id;
    form.querySelectorAll("[data-edit-only]").forEach(function (el) { el.hidden = !hideable; });
    var hideButton = form.querySelector("[data-act=drawer-hide]");
    hideButton.textContent = data.hidden ? "Вернуть в меню" : "Скрыть из меню";
    form.querySelector("[data-hide-hint]").hidden = !hideable || data.hidden;
    form.querySelector("[data-name-label]").textContent =
      data.kind === "item" ? "Название" : "Название раздела";
    drawer.hidden = false;
    scrim.hidden = false;
    setLanguage(form.querySelector("[data-lang=ru]") ? "ru" : "en");
    nameInput.focus();
  }

  function openItem(button, item, sectionId) {
    var sections = board.querySelectorAll("[data-section-id]");
    if (!item && sections.length === 0) {
      toast("Сначала добавьте раздел — например, «Завтраки».");
      return;
    }
    var data = item ? {
      kind: "item", id: item.id, hidden: item.hidden,
      names: Object.assign({}, item.names), descriptions: Object.assign({}, item.descriptions),
      section: item.section_id
    } : {
      kind: "item", id: null, hidden: false, names: {}, descriptions: {},
      section: sectionId || sections[0].dataset.sectionId
    };
    title.textContent = item ? "Изменить товар" : "Новый товар";
    saveButton.textContent = item ? "Сохранить" : "Добавить товар";
    priceInput.value = item ? String(item.price) : "";
    if (photo) photo.reset(item ? item.photo_url : null);
    sectionOptions(data.section);
    openDrawer(button, data);
    setAvailable(item ? item.is_available : true);
  }

  function openSection(button, sectionId, names) {
    title.textContent = sectionId ? "Переименовать раздел" : "Новый раздел";
    saveButton.textContent = sectionId ? "Сохранить" : "Добавить раздел";
    openDrawer(button, {
      kind: "section", id: sectionId, hidden: false,
      names: Object.assign({}, names), descriptions: {}
    });
  }

  function closeDrawer() {
    drawer.hidden = true;
    scrim.hidden = true;
    editing = null;
    if (photo) photo.reset(null);
    if (opener && document.body.contains(opener)) opener.focus();
    opener = null;
  }

  /* Переводы: пустые не отправляются («перевода нет»), а языки, которых нет во
   * вкладках отеля, уходят обратно как были — форма их не стирает. */
  function texts(map) {
    var result = {};
    Object.keys(map).forEach(function (language) {
      var text = (map[language] || "").trim();
      if (text) result[language] = text;
    });
    return result;
  }

  function currentCatalog() {
    var toolbar = board.querySelector("[data-catalog]");
    return toolbar ? toolbar.dataset.catalog : "";
  }

  function buildRequest() {
    setLanguage(editing.language);
    var names = texts(editing.names);
    if (!names.en) {
      setLanguage("en");
      return { error: "Заполните название на английском (вкладка EN) — его видят гости без перевода." };
    }
    if (editing.kind === "section") {
      return {
        path: editing.id ? "/sections/" + editing.id : "/catalogs/" + currentCatalog() + "/sections",
        body: { names: names },
        done: editing.id ? "Раздел переименован" : "Раздел добавлен"
      };
    }
    var price = Number(priceInput.value);
    if (!priceInput.value || !Number.isInteger(price) || price < 1) {
      return { error: "Цена — целое число тенге больше нуля." };
    }
    if (price > MAX_PRICE_TENGE) return { error: "Цена — не больше 1 000 000 ₸." };
    var descriptions = texts(editing.descriptions);
    if (Object.keys(descriptions).length && !descriptions.en) {
      setLanguage("en");
      return { error: "Описание есть на другом языке — добавьте его и на английском (вкладка EN)." };
    }
    return {
      path: editing.id ? "/items/" + editing.id : "/items",
      body: {
        section_id: sectionSelect.value,
        names: names,
        descriptions: Object.keys(descriptions).length ? descriptions : null,
        price_minor: price * MINOR_PER_TENGE,
        is_available: editing.available
      },
      done: editing.id ? "Сохранено" : "Товар добавлен"
    };
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!editing) return;
    var request = buildRequest();
    if (request.error) return showError(request.error);
    saveButton.disabled = true;
    var result = await send(request.path, request.body);
    if (result.ok && editing.kind === "item" && photo && photo.pending()) {
      var label = saveButton.textContent;
      saveButton.textContent = "Загружаем фото…";
      var photoResult = await photo.commit(editing.id || result.data.id);
      saveButton.textContent = label;
      if (!photoResult.ok) {
        /* Поля сохранены: форма остаётся открытой уже как «Изменить товар» —
         * повторное «Сохранить» отправит фото ещё раз, а не второй товар. */
        saveButton.disabled = false;
        editing.id = editing.id || result.data.id;
        title.textContent = "Изменить товар";
        saveButton.textContent = "Сохранить";
        showError("Товар сохранён, а фото — нет: " + failureText(photoResult));
        await refresh();
        return;
      }
    }
    saveButton.disabled = false;
    if (!result.ok) return showError(failureText(result));
    closeDrawer();
    toast(request.done);
    await refresh();
  });

  form.addEventListener("click", async function (event) {
    var tab = event.target.closest("[data-lang]");
    if (tab) return setLanguage(tab.dataset.lang);
    var button = event.target.closest("[data-act]");
    if (!button && event.target.closest("#catalog-available")) {
      return setAvailable(!editing.available);
    }
    if (!button) return;
    if (button.dataset.act === "close") return closeDrawer();
    if (button.dataset.act === "drawer-hide") {
      var hide = !editing.hidden;
      var result = await send("/items/" + editing.id + "/hidden", { hidden: hide });
      if (!result.ok) return showError(failureText(result));
      closeDrawer();
      toast(hide ? "Скрыто из меню" : "Снова в меню");
      await refresh();
    }
  });

  scrim.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    if (!drawer.hidden) closeDrawer();
    else closeMenus();
  });

  applyView();
})();
